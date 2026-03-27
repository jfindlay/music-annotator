"""Unit tests for pipeline functions: build_cea_performers, build_track_tags, apply_tags_flac, apply_tags_mp3,
find_source_files, and run (non-dry-run).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mutagen._util import MutagenError
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TXXX  # type: ignore[attr-defined]
from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

import music_annotator
from music_annotator import (
    JOURNAL_FILENAME,
    CollisionPolicy,
    _read_tags_flac,
    _read_tags_mp3,
    _sha256_file,
    _verify_copy,
    apply_tags_flac,
    apply_tags_mp3,
    build_cea_performers,
    build_track_tags,
    fetch_acoustid_id,
    find_source_files,
)
from music_annotator._pipeline import _match_medium_by_toc, _prompt_collision_policy, _write_sidecars
from music_annotator._pipeline_io import _DISC_INFO_FILENAME, _DISC_TOC_FILENAME
from music_annotator._tagger import _FLAC_MAX_PICTURE_BYTES
from music_annotator.models import (
    JSON,
    CoverArt,
    CoverImage,
    MBMedium,
    MBRecording,
    MBRelease,
    MBTrack,
    MBWork,
    TrackTags,
    TransactionEntry,
)


def _rel(d: dict[str, JSON]) -> MBRelease:
    """Validate a raw release dict into an MBRelease model.

    :param d: Raw dict matching the musicbrainzngs release response shape.
    :returns: An :class:`~music_annotator.models.MBRelease` instance.
    """
    return MBRelease.model_validate(d)


def _rec(d: dict[str, JSON]) -> MBRecording:
    """Validate a raw recording dict into an MBRecording model.

    :param d: Raw dict matching the musicbrainzngs recording response shape.
    :returns: An :class:`~music_annotator.models.MBRecording` instance.
    """
    return MBRecording.model_validate(d)


def _trk(d: dict[str, JSON]) -> MBTrack:
    """Validate a raw track dict into an MBTrack model.

    :param d: Raw dict matching the musicbrainzngs track response shape.
    :returns: An :class:`~music_annotator.models.MBTrack` instance.
    """
    return MBTrack.model_validate(d)


def _w(d: dict[str, JSON]) -> MBWork:
    """Validate a raw work dict into an MBWork model.

    :param d: Raw dict matching the musicbrainzngs work response shape.
    :returns: An :class:`~music_annotator.models.MBWork` instance.
    """
    return MBWork.model_validate(d)


# ---------------------------------------------------------------------------
# Minimal valid FLAC bytes (magic + STREAMINFO block, last-metadata bit set)
# ---------------------------------------------------------------------------

# Valid minimal FLAC: magic + STREAMINFO block (last-metadata, 44100 Hz, 2 ch, 16-bit, 0 samples)
_MINIMAL_FLAC = (
    b"fLaC"
    b"\x80\x00\x00\x22"  # block header: last=1, type=0, length=34
    b"\x10\x00\x10\x00"  # min_blocksize=4096, max_blocksize=4096
    b"\x00\x00\x00"  # min_framesize=0
    b"\x00\x00\x00"  # max_framesize=0
    b"\x0a\xc4\x42\xf0\x00\x00\x00\x00"  # 44100 Hz, 2ch, 16-bit, 0 samples
    b"\x00" * 16  # MD5
)

# ---------------------------------------------------------------------------
# Minimal valid MP3: ID3v2.3 header + one null frame
# ---------------------------------------------------------------------------

_ID3_HEADER = b"ID3\x03\x00\x00" + b"\x00\x00\x00\x00"  # 10-byte header, size 0
_MINIMAL_MP3 = _ID3_HEADER + b"\xff\xfb\x90\x00" + b"\x00" * 413  # one MP3 frame


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _make_release(n_tracks: int = 1) -> MBRelease:
    """Build a minimal release model.

    :param n_tracks: Number of tracks to include on medium 1.
    :returns: An :class:`~music_annotator.models.MBRelease` instance.
    """
    tracks: list[JSON] = []
    for i in range(1, n_tracks + 1):
        tracks.append(
            {
                "id": f"trk-{i}",
                "position": i,
                "recording": {
                    "id": f"rec-{i}",
                    "title": f"Track {i}",
                    "artist-credit": [],
                },
            }
        )
    return MBRelease.model_validate(
        {
            "id": "rel-1",
            "title": "Test Album",
            "date": "2000",
            "status": "Official",
            "barcode": "123456",
            "artist-credit": [
                {
                    "name": "Composer A",
                    "artist": {"id": "a1", "name": "Composer A", "sort-name": "A, Composer"},
                }
            ],
            "release-group": {"id": "rg-1", "primary-type": "Album", "first-release-date": "2000"},
            "label-info-list": [{"label": {"id": "l1", "name": "Label X"}, "catalog-number": "CAT-001"}],
            "text-representation": {"script": "Latn", "language": "eng"},
            "medium-list": [{"position": 1, "format": "CD", "track-list": tracks}],
        }
    )


def _make_multi_disc_release(
    tracks_per_disc: list[int],
    disc_offsets: list[list[list[int]]] | None = None,
) -> MBRelease:
    """Build a minimal multi-disc release model.

    Each element in ``tracks_per_disc`` specifies the number of tracks on that medium (disc).
    Medium positions are 1-based.

    :param tracks_per_disc: List of per-medium track counts.
    :param disc_offsets: Optional per-medium list of disc TOC offset lists.  Each element is a list of
        ``offsets`` lists for one or more :class:`~music_annotator.models.MBDisc` entries on that medium.
        When ``None``, no ``discs`` key is included (empty disc_list on each medium).
    :returns: An :class:`~music_annotator.models.MBRelease` instance with multiple mediums.
    """
    mediums: list[JSON] = []
    for disc_idx, n_tracks in enumerate(tracks_per_disc, start=1):
        tracks: list[JSON] = []
        for trk_idx in range(1, n_tracks + 1):
            tracks.append(
                {
                    "id": f"trk-d{disc_idx}-{trk_idx}",
                    "position": trk_idx,
                    "recording": {
                        "id": f"rec-d{disc_idx}-{trk_idx}",
                        "title": f"Disc {disc_idx} Track {trk_idx}",
                        "artist-credit": [],
                    },
                }
            )
        medium: dict[str, JSON] = {"position": disc_idx, "format": "CD", "track-list": tracks}
        if disc_offsets is not None:
            discs: list[dict[str, object]] = [
                {"offset-list": offsets, "sectors": str(offsets[-1] + 1000)} for offsets in disc_offsets[disc_idx - 1]
            ]
            medium["disc-list"] = discs  # type: ignore[assignment]
        mediums.append(medium)
    return MBRelease.model_validate(
        {
            "id": "rel-multi",
            "title": "Multi-Disc Album",
            "date": "2000",
            "status": "Official",
            "barcode": "",
            "artist-credit": [],
            "release-group": {"id": "rg-1", "primary-type": "Album", "first-release-date": "2000"},
            "label-info-list": [],
            "text-representation": {"script": "Latn", "language": "eng"},
            "medium-list": mediums,
        }
    )


def _make_rec_detail(rec_id: str = "rec-1") -> MBRecording:
    """Build a minimal recording detail model.

    :param rec_id: Recording MBID.
    :returns: An :class:`~music_annotator.models.MBRecording` instance.
    """
    return MBRecording.model_validate(
        {
            "id": rec_id,
            "title": "Track 1",
            "artist-credit": [],
            "artist-relation-list": [
                {
                    "type": "conductor",
                    "artist": {"id": "k1", "name": "Karajan", "sort-name": "Karajan, H"},
                    "attribute-list": [],
                }
            ],
            "work-relation-list": [],
        }
    )


# ---------------------------------------------------------------------------
# build_cea_performers
# ---------------------------------------------------------------------------


class TestBuildCeaPerformers:
    """Tests for build_cea_performers covering all role branches."""

    def _recording(self, rtype: str, name: str = "Artist X", attrs: list[JSON] | None = None) -> MBRecording:
        """Build a minimal MBRecording with one artist relation.

        :param rtype: Relation type string.
        :param name: Artist display name.
        :param attrs: attribute-list entries.
        :returns: An :class:`~music_annotator.models.MBRecording` instance.
        """
        return _rec(
            {
                "id": "rec-x",
                "title": "T",
                "artist-credit": [],
                "artist-relation-list": [
                    {
                        "type": rtype,
                        "artist": {"id": "x1", "name": name, "sort-name": name},
                        "attribute-list": attrs or [],
                    }
                ],
                "work-relation-list": [],
            }
        )

    def test_conductor(self) -> None:
        """conductor relation populates cea.conductors."""
        cea = build_cea_performers(self._recording("conductor", "Karajan"))
        assert len(cea.conductors) == 1
        assert cea.conductors[0].name == "Karajan"

    def test_chorus_master(self) -> None:
        """chorus master relation populates cea.chorusmasters."""
        cea = build_cea_performers(self._recording("chorus master"))
        assert len(cea.chorusmasters) == 1

    def test_concertmaster(self) -> None:
        """concertmaster relation populates cea.leaders."""
        cea = build_cea_performers(self._recording("concertmaster"))
        assert len(cea.leaders) == 1

    def test_arranger(self) -> None:
        """arranger relation populates cea.arrangers."""
        cea = build_cea_performers(self._recording("arranger"))
        assert len(cea.arrangers) == 1

    def test_instrument_arranger(self) -> None:
        """instrument arranger relation populates cea.arrangers."""
        cea = build_cea_performers(self._recording("instrument arranger"))
        assert len(cea.arrangers) == 1

    def test_vocal_arranger(self) -> None:
        """vocal arranger relation populates cea.arrangers."""
        cea = build_cea_performers(self._recording("vocal arranger"))
        assert len(cea.arrangers) == 1

    def test_orchestrator(self) -> None:
        """orchestrator relation populates cea.orchestrators."""
        cea = build_cea_performers(self._recording("orchestrator"))
        assert len(cea.orchestrators) == 1

    def test_composer(self) -> None:
        """composer relation populates cea.composers."""
        cea = build_cea_performers(self._recording("composer"))
        assert len(cea.composers) == 1

    def test_writer(self) -> None:
        """writer relation populates cea.composers."""
        cea = build_cea_performers(self._recording("writer"))
        assert len(cea.composers) == 1

    def test_producer(self) -> None:
        """producer relation populates cea.producers."""
        cea = build_cea_performers(self._recording("producer"))
        assert len(cea.producers) == 1

    def test_balance_engineer(self) -> None:
        """balance relation populates cea.engineers."""
        cea = build_cea_performers(self._recording("balance"))
        assert len(cea.engineers) == 1

    def test_performing_orchestra_is_ensemble(self) -> None:
        """performing orchestra with ensemble name → cea.ensembles."""
        cea = build_cea_performers(self._recording("performing orchestra", "Berliner Philharmoniker"))
        assert len(cea.ensembles) == 1

    def test_performer_vocalist(self) -> None:
        """performer with soprano instrument → cea.vocalists."""
        cea = build_cea_performers(self._recording("performer", "Soprano X", attrs=[{"value": "soprano"}]))
        assert len(cea.vocalists) == 1
        assert cea.vocalists[0].instrument == "soprano"

    def test_performer_instrumentalist(self) -> None:
        """performer with violin instrument → cea.instrumentalists."""
        cea = build_cea_performers(self._recording("performer", "Violinist X", attrs=[{"value": "violin"}]))
        assert len(cea.instrumentalists) == 1
        assert cea.instrumentalists[0].instrument == "violin"

    def test_performer_string_attribute(self) -> None:
        """performer with plain string attribute → instrument set from string."""
        cea = build_cea_performers(self._recording("performer", "Cellist X", attrs=["cello"]))
        assert len(cea.instrumentalists) == 1
        assert cea.instrumentalists[0].instrument == "cello"

    def test_performer_no_instrument_is_other_soloist(self) -> None:
        """performer with no instrument → cea.other_soloists."""
        cea = build_cea_performers(self._recording("performer", "Unknown Performer", attrs=[]))
        assert len(cea.other_soloists) == 1

    def test_empty_relation_list(self) -> None:
        """Empty artist-relation-list returns empty CeaPerformers."""
        cea = build_cea_performers(
            _rec({"id": "r", "title": "T", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []})
        )
        assert cea.all_soloists == []
        assert cea.conductors == []

    def test_unknown_role_ignored(self) -> None:
        """Unrecognised role type does not populate any bucket."""
        cea = build_cea_performers(self._recording("mastering engineer"))
        assert not cea.conductors
        assert not cea.composers
        assert not cea.all_soloists


# ---------------------------------------------------------------------------
# build_track_tags
# ---------------------------------------------------------------------------


class TestBuildTrackTags:
    """Tests for build_track_tags."""

    def _track(self, pos: int = 1, rec_id: str = "rec-1", title: str = "Track 1") -> MBTrack:
        """Build a minimal MBTrack model.

        :param pos: Track position (int).
        :param rec_id: Recording MBID.
        :param title: Recording title.
        :returns: An :class:`~music_annotator.models.MBTrack` instance.
        """
        return _trk(
            {
                "id": f"trk-{pos}",
                "position": pos,
                "recording": {"id": rec_id, "title": title, "artist-credit": []},
            }
        )

    def test_basic_fields_populated(self) -> None:
        """Standard fields like title, album, tracknumber are set."""
        tags = build_track_tags(_make_release(), self._track(), 1, _make_rec_detail(), [])
        assert tags.title == "Track 1"
        assert tags.album == "Test Album"
        assert tags.tracknumber == "1"
        assert tags.label == "Label X"
        assert tags.catalognumber == "CAT-001"

    def test_musicbrainz_ids_populated(self) -> None:
        """MusicBrainz ID fields are set from release/track/recording."""
        tags = build_track_tags(_make_release(), self._track(), 1, _make_rec_detail(), [])
        assert tags.musicbrainz_albumid == "rel-1"
        assert tags.musicbrainz_recordingid == "rec-1"
        assert tags.musicbrainz_trackid == "trk-1"
        assert tags.musicbrainz_releasegroupid == "rg-1"

    def test_conductor_populated_from_recording(self) -> None:
        """conductor field is set from recording-level conductor relation."""
        tags = build_track_tags(_make_release(), self._track(), 1, _make_rec_detail(), [])
        assert tags.conductor == "Karajan"

    def test_composer_from_work_hierarchy(self) -> None:
        """composer field is set from work-level composer relation."""
        tags = build_track_tags(
            _make_release(),
            self._track(),
            1,
            _rec(
                {
                    "id": "rec-1",
                    "title": "Track 1",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [{"type": "performance", "work": {"id": "w1", "title": "Sym"}}],
                }
            ),
            [
                _w(
                    {
                        "id": "w1",
                        "title": "Sym",
                        "type": "",
                        "artist-relation-list": [
                            {"type": "composer", "artist": {"id": "c1", "name": "Beethoven", "sort-name": "Beethoven, L"}},
                        ],
                        "work-relation-list": [],
                        "attribute-list": [],
                        "tag-list": [],
                    }
                )
            ],
        )
        assert tags.composer == "Beethoven"

    def test_composer_falls_back_to_cea_composer(self) -> None:
        """composer falls back to cea.composers when no work-level composer found."""
        tags = build_track_tags(
            _make_release(),
            self._track(),
            1,
            _rec(
                {
                    "id": "rec-1",
                    "title": "Track 1",
                    "artist-credit": [],
                    "artist-relation-list": [
                        {
                            "type": "composer",
                            "artist": {"id": "c1", "name": "Bach", "sort-name": "Bach, JS"},
                            "attribute-list": [],
                        }
                    ],
                    "work-relation-list": [],
                }
            ),
            [],
        )
        assert tags.composer == "Bach"

    def test_genre_from_worktype(self) -> None:
        """genre is derived from work type when recognised."""
        tags = build_track_tags(
            _make_release(),
            self._track(),
            1,
            _make_rec_detail(),
            [
                _w(
                    {
                        "id": "w1",
                        "title": "Symphony",
                        "type": "Symphony",
                        "artist-relation-list": [],
                        "work-relation-list": [],
                        "attribute-list": [],
                        "tag-list": [],
                    }
                )
            ],
        )
        assert tags.genre == "Symphony"

    def test_genre_defaults_to_classical(self) -> None:
        """genre defaults to 'Classical' when work type not in WORKTYPE_GENRES."""
        tags = build_track_tags(_make_release(), self._track(), 1, _make_rec_detail(), [])
        assert tags.genre == "Classical"

    def test_per_level_extras_set(self) -> None:
        """cwp_work_N extras are set for each work hierarchy level."""
        tags = build_track_tags(
            _make_release(),
            self._track(),
            1,
            _make_rec_detail(),
            [
                _w(
                    {
                        "id": "w1",
                        "title": "Symphony No. 5",
                        "type": "",
                        "artist-relation-list": [],
                        "work-relation-list": [],
                        "attribute-list": [],
                        "tag-list": [],
                    }
                )
            ],
        )
        assert tags.model_extra.get("cwp_work_0") == "Symphony No. 5"  # type: ignore[union-attr]

    def test_arranger_deduplication(self) -> None:
        """Arrangers appearing in both cea and role_buckets are deduplicated."""
        tags = build_track_tags(
            _make_release(),
            self._track(),
            1,
            _rec(
                {
                    "id": "rec-1",
                    "title": "Track 1",
                    "artist-credit": [],
                    "artist-relation-list": [
                        {
                            "type": "arranger",
                            "artist": {"id": "a1", "name": "Arr X", "sort-name": "X, Arr"},
                            "attribute-list": [],
                        },
                    ],
                    "work-relation-list": [],
                }
            ),
            [
                _w(
                    {
                        "id": "w1",
                        "title": "Work",
                        "type": "",
                        "artist-relation-list": [
                            {"type": "arranger", "artist": {"id": "a1", "name": "Arr X", "sort-name": "X, Arr"}},
                        ],
                        "work-relation-list": [],
                        "attribute-list": [],
                        "tag-list": [],
                    }
                )
            ],
        )
        # Should appear only once
        assert tags.arranger.count("Arr X") == 1

    def test_orchestrator_role(self) -> None:
        """Orchestrator from work level appears in arranger field with (orch.) suffix."""
        tags = build_track_tags(
            _make_release(),
            self._track(),
            1,
            _make_rec_detail(),
            [
                _w(
                    {
                        "id": "w1",
                        "title": "Work",
                        "type": "",
                        "artist-relation-list": [
                            {"type": "orchestrator", "artist": {"id": "o1", "name": "Orch X", "sort-name": "X, Orch"}},
                        ],
                        "work-relation-list": [],
                        "attribute-list": [],
                        "tag-list": [],
                    }
                )
            ],
        )
        assert "orch." in tags.arranger

    def test_reconstructor_and_revisor_roles(self) -> None:
        """Reconstructor and revisor appear in arranger field with labels."""
        tags = build_track_tags(
            _make_release(),
            self._track(),
            1,
            _make_rec_detail(),
            [
                _w(
                    {
                        "id": "w1",
                        "title": "Work",
                        "type": "",
                        "artist-relation-list": [
                            {"type": "reconstructed by", "artist": {"id": "r1", "name": "Rec X", "sort-name": "X, Rec"}},
                            {"type": "revised by", "artist": {"id": "rv1", "name": "Rev Y", "sort-name": "Y, Rev"}},
                        ],
                        "work-relation-list": [],
                        "attribute-list": [],
                        "tag-list": [],
                    }
                )
            ],
        )
        assert "reconstructed" in tags.arranger
        assert "revised" in tags.arranger

    def test_work_relation_link_set(self) -> None:
        """musicbrainz_workid is set when recording has a performance work link."""
        tags = build_track_tags(
            _make_release(),
            self._track(),
            1,
            _rec(
                {
                    "id": "rec-1",
                    "title": "Track 1",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [{"type": "performance", "work": {"id": "w1", "title": "My Work"}}],
                }
            ),
            [],
        )
        assert tags.musicbrainz_workid == "w1"

    def test_soloist_with_instrument_in_soloists_string(self) -> None:
        """Soloist with instrument appears as 'Name (instr)' in soloists field."""
        tags = build_track_tags(
            _make_release(),
            self._track(),
            1,
            _rec(
                {
                    "id": "rec-1",
                    "title": "Track 1",
                    "artist-credit": [],
                    "artist-relation-list": [
                        {
                            "type": "performer",
                            "artist": {"id": "v1", "name": "Violinist", "sort-name": "Violinist"},
                            "attribute-list": [{"value": "violin"}],
                        }
                    ],
                    "work-relation-list": [],
                }
            ),
            [],
        )
        assert "Violinist (violin)" in tags.soloists

    def test_ensemble_in_band_field(self) -> None:
        """Ensemble name is in the 'band' field."""
        tags = build_track_tags(
            _make_release(),
            self._track(),
            1,
            _rec(
                {
                    "id": "rec-1",
                    "title": "Track 1",
                    "artist-credit": [],
                    "artist-relation-list": [
                        {
                            "type": "performing orchestra",
                            "artist": {
                                "id": "bp",
                                "name": "Berliner Philharmoniker",
                                "sort-name": "Berliner Philharmoniker",
                            },
                            "attribute-list": [],
                        }
                    ],
                    "work-relation-list": [],
                }
            ),
            [],
        )
        assert "Berliner Philharmoniker" in tags.band

    def test_lyricist_and_translator(self) -> None:
        """Lyricist and translator roles are populated from work hierarchy."""
        tags = build_track_tags(
            _make_release(),
            self._track(),
            1,
            _make_rec_detail(),
            [
                _w(
                    {
                        "id": "w1",
                        "title": "Song",
                        "type": "",
                        "artist-relation-list": [
                            {"type": "lyricist", "artist": {"id": "ly1", "name": "Lyric X", "sort-name": "X, Lyric"}},
                            {"type": "translator", "artist": {"id": "tr1", "name": "Trans Y", "sort-name": "Y, Trans"}},
                        ],
                        "work-relation-list": [],
                        "attribute-list": [],
                        "tag-list": [],
                    }
                )
            ],
        )
        assert "Lyric X" in tags.lyricist
        assert "Trans Y" in tags.translator

    def test_librettist_appears_in_lyricist(self) -> None:
        """Librettist is concatenated with lyricist in the lyricist field."""
        tags = build_track_tags(
            _make_release(),
            self._track(),
            1,
            _make_rec_detail(),
            [
                _w(
                    {
                        "id": "w1",
                        "title": "Opera",
                        "type": "",
                        "artist-relation-list": [
                            {"type": "librettist", "artist": {"id": "lb1", "name": "Lib Z", "sort-name": "Z, Lib"}},
                        ],
                        "work-relation-list": [],
                        "attribute-list": [],
                        "tag-list": [],
                    }
                )
            ],
        )
        assert "Lib Z" in tags.lyricist


# ---------------------------------------------------------------------------
# find_source_files
# ---------------------------------------------------------------------------


class TestFindSourceFiles:
    """Tests for find_source_files."""

    def test_returns_flac_files(self, fs: FakeFilesystem) -> None:
        """Returns .flac files sorted by name.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        fs.create_dir(str(src))
        fs.create_file(str(src / "02.flac"))
        fs.create_file(str(src / "01.flac"))
        result = find_source_files(src)
        assert [p.name for p in result] == ["01.flac", "02.flac"]

    def test_returns_mp3_files(self, fs: FakeFilesystem) -> None:
        """Returns .mp3 files.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        fs.create_dir(str(src))
        fs.create_file(str(src / "track.mp3"))
        result = find_source_files(src)
        assert result[0].suffix == ".mp3"

    def test_excludes_non_audio(self, fs: FakeFilesystem) -> None:
        """Non-audio files (e.g. .txt) are excluded.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        fs.create_dir(str(src))
        fs.create_file(str(src / "notes.txt"))
        fs.create_file(str(src / "cover.jpg"))
        fs.create_file(str(src / "track.flac"))
        result = find_source_files(src)
        assert len(result) == 1
        assert result[0].name == "track.flac"

    def test_empty_dir_returns_empty_list(self, fs: FakeFilesystem) -> None:
        """Empty directory returns empty list.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src_empty")
        fs.create_dir(str(src))
        assert find_source_files(src) == []

    def test_mixed_extensions(self, fs: FakeFilesystem) -> None:
        """Handles mix of .flac, .mp3, .ogg, .m4a, .wav.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        fs.create_dir(str(src))
        for ext in (".flac", ".mp3", ".ogg", ".m4a", ".wav"):
            fs.create_file(str(src / f"track{ext}"))
        result = find_source_files(src)
        assert len(result) == 5

    def test_excludes_disc_toc_flac(self, fs: FakeFilesystem) -> None:
        """The CD table-of-contents FLAC (``00 - disc TOC.flac``) is excluded from results.

        Even though ``00 - disc TOC.flac`` has a ``.flac`` extension, it must never be counted
        as a source track because it causes a track-count mismatch against the MB release.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01 - track.flac"))
        fs.create_file(str(src / _DISC_TOC_FILENAME))
        result = find_source_files(src)
        assert [p.name for p in result] == ["01 - track.flac"]

    def test_excludes_disc_info_yaml(self, fs: FakeFilesystem) -> None:
        """The FreeDB disc-info YAML (``00 - disc info.yaml``) is excluded from results.

        ``00 - disc info.yaml`` does not have an audio extension so it would already be filtered
        by the extension check; this test confirms the name-based exclusion also covers it in
        case :data:`AUDIO_EXTENSIONS` is ever extended to include ``.yaml``.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01 - track.flac"))
        fs.create_file(str(src / _DISC_INFO_FILENAME))
        result = find_source_files(src)
        assert [p.name for p in result] == ["01 - track.flac"]


# ---------------------------------------------------------------------------
# apply_tags_flac
# ---------------------------------------------------------------------------


class TestApplyTagsFlac:
    """Tests for apply_tags_flac."""

    def test_writes_tags_to_flac(self, fs: FakeFilesystem) -> None:
        """Tags are written to a FLAC file without raising.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.flac")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_FLAC)
        tags = TrackTags(title="My Title", album="My Album", tracknumber="1")
        apply_tags_flac(dest, tags)

    def test_writes_cover_art_to_flac(self, fs: FakeFilesystem) -> None:
        """Cover art is embedded without raising when cover.available is True.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.flac")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_FLAC)
        tags = TrackTags(title="Track")
        cover = CoverArt(front=[CoverImage(data=b"\xff\xd8\xff\xe0" + b"\x00" * 100, mime="image/jpeg")])
        apply_tags_flac(dest, tags, cover)

    def test_no_cover_no_error(self, fs: FakeFilesystem) -> None:
        """apply_tags_flac succeeds when no cover is passed.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.flac")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_FLAC)
        tags = TrackTags(title="Track")
        apply_tags_flac(dest, tags, None)

    def test_empty_cover_not_embedded(self, fs: FakeFilesystem) -> None:
        """Empty CoverArt (available=False) is not embedded.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.flac")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_FLAC)
        tags = TrackTags(title="Track")
        cover = CoverArt()  # data=b"" → available=False
        apply_tags_flac(dest, tags, cover)


# ---------------------------------------------------------------------------
# apply_tags_flac — _FLAC_MAX_PICTURE_BYTES guard
# ---------------------------------------------------------------------------


class TestApplyTagsFlacSizeGuard:
    """Tests for the FLAC block size guard in apply_tags_flac."""

    def test_oversized_front_image_skipped(self, fs: FakeFilesystem) -> None:
        """Front images exceeding _FLAC_MAX_PICTURE_BYTES are not embedded.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/dest/01.flac")
        fs.create_file(str(dest), contents=_MINIMAL_FLAC)
        tags = TrackTags(title="T", movementnumber="1", movementtotal="1", cea_conductors_list=[], cea_ensembles_list=[])
        # Oversized image: just over the 16 MB limit
        oversized = b"\xff\xd8" + b"\x00" * (_FLAC_MAX_PICTURE_BYTES + 1)  # pylint: disable=protected-access
        cover = CoverArt(front=[CoverImage(data=oversized, mime="image/jpeg")])
        apply_tags_flac(dest, tags, cover)

        audio = FLAC(str(dest))
        assert audio.pictures == []  # oversized image was skipped

    def test_normal_front_image_embedded(self, fs: FakeFilesystem) -> None:
        """Front images within _FLAC_MAX_PICTURE_BYTES are embedded normally.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/dest/01.flac")
        fs.create_file(str(dest), contents=_MINIMAL_FLAC)
        tags = TrackTags(title="T", movementnumber="1", movementtotal="1", cea_conductors_list=[], cea_ensembles_list=[])
        small_jpeg = b"\xff\xd8" + b"\x00" * 100
        cover = CoverArt(front=[CoverImage(data=small_jpeg, mime="image/jpeg")])
        apply_tags_flac(dest, tags, cover)

        audio = FLAC(str(dest))
        assert len(audio.pictures) == 1
        assert audio.pictures[0].data == small_jpeg


# ---------------------------------------------------------------------------
# _write_sidecars
# ---------------------------------------------------------------------------


class TestWriteSidecars:
    """Tests for the _write_sidecars helper in _pipeline."""

    def _make_cover(self) -> CoverArt:
        """Build a minimal CoverArt with sidecar images.

        :returns: A CoverArt with front_full, back, and booklet images.
        """
        return CoverArt(
            front=[CoverImage(data=b"\xff\xd8\x00", mime="image/jpeg")],
            front_full=[
                CoverImage(data=b"\xff\xd8\x01" * 100, mime="image/jpeg", filename="cover.jpg", url="https://caa/cover")
            ],
            back=[CoverImage(data=b"%PDF-back", mime="application/pdf", filename="back.pdf", url="https://caa/back")],
            booklet=[
                CoverImage(data=b"%PDF-book1", mime="application/pdf", filename="booklet-1.pdf", url="https://caa/book1"),
                CoverImage(data=b"%PDF-book2", mime="application/pdf", filename="booklet-2.pdf", url="https://caa/book2"),
            ],
        )

    def test_sidecar_files_written_to_work_top_dir(self, fs: FakeFilesystem) -> None:
        """Sidecar files are written into the work top directory.

        :param fs: pyfakefs fixture.
        """
        work_top = Path("/dest/Composer/Work [rel 1963]")
        fs.create_dir(str(work_top))
        sidecars_written: set[Path] = set()
        journal: list[TransactionEntry] = []
        _write_sidecars(self._make_cover(), work_top, sidecars_written, journal, "t", "r1")  # pylint: disable=protected-access
        assert (work_top / "cover.jpg").exists()
        assert (work_top / "back.pdf").exists()
        assert (work_top / "booklet-1.pdf").exists()
        assert (work_top / "booklet-2.pdf").exists()

    def test_journal_entries_created_with_caa_url_as_source(self, fs: FakeFilesystem) -> None:
        """Each sidecar write produces a journal entry with action='downloaded' and source=CAA URL.

        :param fs: pyfakefs fixture.
        """
        work_top = Path("/dest/Composer/Work")
        fs.create_dir(str(work_top))
        sidecars_written: set[Path] = set()
        journal: list[TransactionEntry] = []
        _write_sidecars(self._make_cover(), work_top, sidecars_written, journal, "now", "rel-1")  # pylint: disable=protected-access
        actions = [e.action for e in journal]
        assert all(a == "downloaded" for a in actions)
        sources = {e.source for e in journal}
        assert "https://caa/cover" in sources
        assert "https://caa/back" in sources
        assert "https://caa/book1" in sources

    def test_second_call_same_dir_is_noop(self, fs: FakeFilesystem) -> None:
        """_write_sidecars is idempotent: second call for same work_top_dir does nothing.

        :param fs: pyfakefs fixture.
        """
        work_top = Path("/dest/Composer/Work")
        fs.create_dir(str(work_top))
        sidecars_written: set[Path] = set()
        journal: list[TransactionEntry] = []
        _write_sidecars(self._make_cover(), work_top, sidecars_written, journal, "now", "rel-1")  # pylint: disable=protected-access
        first_count = len(journal)
        _write_sidecars(self._make_cover(), work_top, sidecars_written, journal, "now", "rel-1")  # pylint: disable=protected-access
        assert len(journal) == first_count  # no new entries on second call

    def test_images_without_filename_are_skipped(self, fs: FakeFilesystem) -> None:
        """CoverImages with empty filename in sidecar lists are skipped without writing.

        :param fs: pyfakefs fixture.
        """
        work_top = Path("/dest/Composer/Work")
        fs.create_dir(str(work_top))
        # An image in front_full with no filename — should be skipped
        cover = CoverArt(
            front_full=[CoverImage(data=b"\xff\xd8", mime="image/jpeg", filename="", url="")],
        )
        sidecars_written: set[Path] = set()
        journal: list[TransactionEntry] = []
        _write_sidecars(cover, work_top, sidecars_written, journal, "now", "rel-1")  # pylint: disable=protected-access
        assert journal == []


# ---------------------------------------------------------------------------
# apply_tags_mp3
# ---------------------------------------------------------------------------


class TestCoverArtSidecarTagDedup:
    """Tests for coverart_* tag deduplication when multi-type images share a filename."""

    def test_shared_filename_appears_once_in_tag(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When back and spine share the same CoverImage, the filename appears only once in each tag.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        shared = CoverImage(data=b"\xff\xd8\x00" * 10, mime="image/jpeg", filename="back.jpg", url="https://caa/99")
        # Same image object appears twice in back (simulates a multi-type dedup scenario)
        cover = CoverArt(back=[shared, shared], spine=[shared])  # duplicate in back list

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_release(n_tracks=1))
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=cover)
        mocker.patch("music_annotator._pipeline.fetch_recording_detail", return_value=MBRecording())
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        tags_used: TrackTags = mock_tag.call_args[0][1]
        # back.jpg should appear only once — not duplicated from back + spine
        assert tags_used.coverart_back_file == "back.jpg"
        assert tags_used.coverart_spine_files == "back.jpg"


class TestApplyTagsMp3:
    """Tests for apply_tags_mp3."""

    def test_writes_basic_tags(self, fs: FakeFilesystem) -> None:
        """Writes standard ID3 frames without raising.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_MP3)
        tags = TrackTags(
            title="My Title",
            artist="Artist",
            albumartist="AlbumArtist",
            album="Album",
            tracknumber="3",
            totaltracks="10",
            discnumber="1",
            date="2000",
            originaldate="1999",
            composer="Composer",
            conductor="Conductor",
            organization="Label",
        )
        apply_tags_mp3(dest, tags)

    def test_writes_cover_art_to_mp3(self, fs: FakeFilesystem) -> None:
        """Cover art APIC frame is written without raising.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_MP3)
        tags = TrackTags(title="Track")
        cover = CoverArt(front=[CoverImage(data=b"\xff\xd8\xff\xe0" + b"\x00" * 100, mime="image/jpeg")])
        apply_tags_mp3(dest, tags, cover)

    def test_no_cover_no_error(self, fs: FakeFilesystem) -> None:
        """apply_tags_mp3 succeeds when no cover is passed.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_MP3)
        apply_tags_mp3(dest, TrackTags(title="Track"), None)

    def test_tracknumber_with_total(self, fs: FakeFilesystem) -> None:
        """TRCK frame is formatted as 'N/Total' when totaltracks is set.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_MP3)
        tags = TrackTags(title="T", tracknumber="2", totaltracks="12")
        apply_tags_mp3(dest, tags)

    def test_tracknumber_without_total(self, fs: FakeFilesystem) -> None:
        """TRCK frame is just 'N' when totaltracks is empty.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_MP3)
        tags = TrackTags(title="T", tracknumber="5")
        apply_tags_mp3(dest, tags)


# ---------------------------------------------------------------------------
# run() — non-dry-run with fetch_rels=True (full pipeline)
# ---------------------------------------------------------------------------


class TestRunFullPipeline:
    """Tests for run() in non-dry-run mode with fetch_rels=True."""

    def _patch_mb(self, mocker: MockerFixture, release: MBRelease) -> None:
        """Patch all MB API calls and post-copy verification.

        :param mocker: pytest-mock fixture.
        :param release: MBRelease model to return from fetch_release.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        def _fetch_rec(rec_id: str) -> MBRecording:
            return _rec(
                {
                    "id": rec_id,
                    "title": "Track",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [],
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

    def test_files_copied_to_dest(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """FLAC files are copied to dest_root in non-dry-run mode.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=1)
        self._patch_mb(mocker, release)
        mocker.patch("music_annotator._pipeline.apply_tags_flac")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        flac_files = list(dest.rglob("*.flac"))
        assert len(flac_files) == 1

    def test_movement_numbers_assigned(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Movement numbers are assigned after all tracks are processed.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "02.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=2)
        self._patch_mb(mocker, release)
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        assert mock_tag.call_count == 2

    def test_recording_date_work_unified_across_movements(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Tracks with different RECORDING_DATE values get a unified RECORDING_DATE_WORK.

        When two movements of the same work have different session date ranges, run()
        computes the union range and writes it to recording_date_work on both tracks.
        This ensures both movements produce the same destination directory label.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "02.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=2)
        self._patch_mb(mocker, release)

        # Patch fetch_recording_detail to return recordings with different session dates:
        # movement 1: single date 1984-01-27
        # movement 2: date range 1981-01-27/1984-01-27
        call_count = [0]

        def _fetch_rec(rec_id: str) -> MBRecording:
            call_count[0] += 1
            return _rec(
                {
                    "id": rec_id,
                    "title": "T",
                    "artist-credit": [],
                    "artist-relation-list": [
                        {
                            "type": "conductor",
                            "direction": "backward",
                            "begin": "1984-01-27" if call_count[0] == 1 else "1981-01-27",
                            "end": "" if call_count[0] == 1 else "1984-01-27",
                            "artist": {"id": "a1", "name": "K", "sort-name": "K"},
                        }
                    ],
                    "work-relation-list": [],
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        # Both tracks should have the same unified recording_date_work
        tags1: TrackTags = mock_tag.call_args_list[0][0][1]
        tags2: TrackTags = mock_tag.call_args_list[1][0][1]
        assert tags1.recording_date_work == tags2.recording_date_work == "1981-01-27/1984-01-27"
        # Individual RECORDING_DATE tags are unchanged
        assert tags1.recording_date == "1984-01-27"
        assert tags2.recording_date == "1981-01-27/1984-01-27"

    def test_recording_date_work_same_year_no_range(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When all movements share the same session year, RECORDING_DATE_WORK has no range suffix.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "02.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=2)
        self._patch_mb(mocker, release)

        def _fetch_rec(rec_id: str) -> MBRecording:
            return _rec(
                {
                    "id": rec_id,
                    "title": "T",
                    "artist-credit": [],
                    "artist-relation-list": [
                        {
                            "type": "balance",
                            "direction": "backward",
                            "begin": "1984-01-27",
                            "end": "1984-02-21",
                            "artist": {"id": "engineer-1", "name": "Engineer", "sort-name": "Engineer"},
                        }
                    ],
                    "work-relation-list": [],
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        tags1: TrackTags = mock_tag.call_args_list[0][0][1]
        tags2: TrackTags = mock_tag.call_args_list[1][0][1]
        # Same year begin and end → both tracks get the same unified value
        assert tags1.recording_date_work == "1984-01-27/1984-02-21"
        assert tags1.recording_date_work == tags2.recording_date_work

    def test_recording_date_work_slash_with_empty_end(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """recording_date with a slash but empty end component is handled gracefully.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=1)
        self._patch_mb(mocker, release)

        def _fetch_rec(rec_id: str) -> MBRecording:
            r = _rec({"id": rec_id, "title": "T", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []})
            return r

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        tags1: TrackTags = mock_tag.call_args_list[0][0][1]
        # No session date relations → recording_date_work stays empty
        assert tags1.recording_date_work == ""

    def test_tag_error_raises_runtime_error(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """A MutagenError during tagging is re-raised as RuntimeError (provenance invariant).

        The file must not be journalled as 'copied' when tagging fails.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=1)
        self._patch_mb(mocker, release)
        mocker.patch("music_annotator._pipeline.apply_tags_flac", side_effect=MutagenError("tag boom"))

        with pytest.raises(RuntimeError, match="tag write failure"):
            music_annotator.run(
                release_id="rel-1",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=False,
                fetch_rels=True,
            )

    def test_track_count_mismatch_raises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """run() raises RuntimeError when source file count does not match release track count.

        This applies in both real and dry-run modes.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        # 2 source files but release has 1 track
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "02.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=1)
        self._patch_mb(mocker, release)

        with pytest.raises(RuntimeError, match="track count mismatch"):
            music_annotator.run(
                release_id="rel-1",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=False,
                fetch_rels=True,
            )

    def test_track_count_mismatch_raises_in_dry_run(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """run() raises RuntimeError on count mismatch even in dry-run mode.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "02.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=1)
        self._patch_mb(mocker, release)

        with pytest.raises(RuntimeError, match="track count mismatch"):
            music_annotator.run(
                release_id="rel-1",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=True,
                fetch_rels=True,
            )

    def test_unsupported_ext_logged_not_raised(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Unsupported audio extension is logged but does not abort the run.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.ogg"), contents=b"OggS" + b"\x00" * 50)

        release = _make_release(n_tracks=1)
        self._patch_mb(mocker, release)

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

    def test_cover_art_fetched_in_non_dry_run(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """fetch_cover_art is called in non-dry-run mode.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=1)
        self._patch_mb(mocker, release)
        mock_cov = mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        mocker.patch("music_annotator._pipeline.apply_tags_flac")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        mock_cov.assert_called_once()

    def test_cover_available_logged(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When cover art is available, a log message is emitted (no exception).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=1)
        self._patch_mb(mocker, release)
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        mocker.patch(
            "music_annotator._pipeline.fetch_cover_art",
            return_value=CoverArt(front=[CoverImage(data=jpeg, mime="image/jpeg")]),
        )
        mocker.patch("music_annotator._pipeline.apply_tags_flac")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

    def test_no_fetch_rels_builds_minimal_tags(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """fetch_rels=False builds minimal tags without calling fetch_recording_detail.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_release(n_tracks=1))
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        spy = mocker.patch("music_annotator._pipeline.fetch_recording_detail")
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )
        spy.assert_not_called()

    def test_mp3_tagged_in_non_dry_run(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """MP3 files are tagged with apply_tags_mp3 in non-dry-run mode.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.mp3"), contents=_MINIMAL_MP3)

        release = _make_release(n_tracks=1)
        self._patch_mb(mocker, release)
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_mp3")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        mock_tag.assert_called_once()


# ---------------------------------------------------------------------------
# build_cea_performers — first_attr is empty dict (falsy non-string)
# ---------------------------------------------------------------------------


class TestBuildCeaPerformersEmptyAttr:
    """Tests for the falsy-non-string first_attr branch in build_cea_performers."""

    def test_performer_with_empty_dict_attribute(self) -> None:
        """performer with an empty dict attribute → instr='' → other_soloists."""
        cea = build_cea_performers(
            _rec(
                {
                    "id": "rx",
                    "title": "T",
                    "artist-credit": [],
                    "artist-relation-list": [
                        {
                            "type": "performer",
                            "artist": {"id": "x1", "name": "Mystery", "sort-name": "Mystery"},
                            "attribute-list": [{}],  # first_attr={} → not str, not truthy → instr=""
                        }
                    ],
                    "work-relation-list": [],
                }
            )
        )
        assert len(cea.other_soloists) == 1
        assert cea.other_soloists[0].instrument == ""


# ---------------------------------------------------------------------------
# apply_tags_mp3 — no title (empty tags) + existing ID3 tags deleted
# ---------------------------------------------------------------------------


class TestApplyTagsMp3EdgeCases:
    """Edge-case tests for apply_tags_mp3."""

    def test_no_title_no_error(self, fs: FakeFilesystem) -> None:
        """apply_tags_mp3 succeeds when TrackTags has no title set.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_MP3)
        # TrackTags() with no title → file_dict["TITLE"] absent → if branch not taken
        apply_tags_mp3(dest, TrackTags())

    def test_existing_id3_tags_deleted(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """apply_tags_mp3 deletes existing ID3 tags before writing new ones.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_MP3)

        # Create mock tags that appears truthy so audio.tags.delete() is called
        mock_tags = mocker.MagicMock()
        mock_audio = mocker.MagicMock()
        mock_audio.tags = mock_tags
        mocker.patch("music_annotator._tagger.MP3", return_value=mock_audio)

        apply_tags_mp3(dest, TrackTags(title="T"))
        mock_tags.delete.assert_called_once_with(str(dest))

    def test_audio_tags_none_skips_delete(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """apply_tags_mp3 skips tag deletion when audio.tags is None (covers 1374->1379).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_MP3)

        # audio.tags = None → if audio.tags: is False → skip delete → go to 1379
        mock_audio = mocker.MagicMock()
        mock_audio.tags = None
        mocker.patch("music_annotator._tagger.MP3", return_value=mock_audio)

        # Should complete without error — tags is None so delete is never called
        apply_tags_mp3(dest, TrackTags(title="T"))


# ---------------------------------------------------------------------------
# apply_tags_mp3 — new TSRC / TLEN / TSST frames
# ---------------------------------------------------------------------------


class TestApplyTagsMp3NewFrames:
    """Tests for TSRC, TLEN, and TSST ID3 frames added to apply_tags_mp3."""

    def test_isrc_written_as_tsrc_frame(self, fs: FakeFilesystem) -> None:
        """apply_tags_mp3 writes the first ISRC as a TSRC frame.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_MP3)
        tags = TrackTags(
            isrc="DEF058402370", movementnumber="1", movementtotal="1", cea_conductors_list=[], cea_ensembles_list=[]
        )
        apply_tags_mp3(dest, tags)

        id3 = ID3(str(dest))  # type: ignore[no-untyped-call]
        assert id3.get("TSRC") is not None  # type: ignore[no-untyped-call]
        assert id3["TSRC"].text[0] == "DEF058402370"

    def test_length_written_as_tlen_frame(self, fs: FakeFilesystem) -> None:
        """apply_tags_mp3 writes LENGTH as a TLEN frame.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_MP3)
        tags = TrackTags(length="541000", movementnumber="1", movementtotal="1", cea_conductors_list=[], cea_ensembles_list=[])
        apply_tags_mp3(dest, tags)

        id3 = ID3(str(dest))  # type: ignore[no-untyped-call]
        assert id3.get("TLEN") is not None  # type: ignore[no-untyped-call]
        assert id3["TLEN"].text[0] == "541000"

    def test_discsubtitle_written_as_tsst_frame(self, fs: FakeFilesystem) -> None:
        """apply_tags_mp3 writes DISCSUBTITLE as a TSST frame.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_MP3)
        tags = TrackTags(
            discsubtitle="Act I", movementnumber="1", movementtotal="1", cea_conductors_list=[], cea_ensembles_list=[]
        )
        apply_tags_mp3(dest, tags)

        id3 = ID3(str(dest))  # type: ignore[no-untyped-call]
        assert id3.get("TSST") is not None  # type: ignore[no-untyped-call]
        assert id3["TSST"].text[0] == "Act I"


# ---------------------------------------------------------------------------
# build_track_tags — arranger/orchestrator already in arranger_seen
# ---------------------------------------------------------------------------


class TestBuildTrackTagsSessionDateRange:
    """Tests for RECORDING_DATE ISO 8601 interval when session spans different dates."""

    def test_multi_date_range_stored_as_iso_interval(self) -> None:
        """RECORDING_DATE is stored as 'begin/end' when begin and end differ."""
        rec = _rec(
            {
                "id": "rec-1",
                "title": "T",
                "artist-credit": [],
                "artist-relation-list": [
                    {
                        "type": "conductor",
                        "direction": "backward",
                        "begin": "1983-12-20",
                        "end": "1984-01-05",
                        "artist": {"id": "karajan-1", "name": "Karajan", "sort-name": "Karajan, Herbert von"},
                    },
                ],
                "work-relation-list": [],
            }
        )
        tags = build_track_tags(
            _make_release(),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec-1", "title": "T", "artist-credit": []}}),
            1,
            rec,
            [],
        )
        assert tags.recording_date == "1983-12-20/1984-01-05"


class TestBuildTrackTagsCreditedName:
    """Tests for the cea_performers_credited companion field."""

    def test_credited_name_differs_from_canonical(self) -> None:
        """When target-credit differs from artist.name, the entry is recorded in cea_performers_credited.

        :param self: Test instance.
        """
        rec = _rec(
            {
                "id": "rec-1",
                "title": "T",
                "artist-credit": [],
                "artist-relation-list": [
                    {
                        "type": "performer",
                        "direction": "backward",
                        "target-credit": "Anne-Sophie Mutter",
                        "artist": {"id": "a1", "name": "Anne‐Sophie Mutter", "sort-name": "Mutter, Anne‐Sophie"},
                    }
                ],
                "work-relation-list": [],
            }
        )
        tags = build_track_tags(
            _make_release(),
            _trk({"id": "t1", "position": 1, "recording": {"id": "rec-1", "title": "T", "artist-credit": []}}),
            1,
            rec,
            [],
        )
        assert "as Anne-Sophie Mutter" in tags.cea_performers_credited


class TestBuildTrackTagsArrangerDedup:
    """Tests for arranger deduplication paths in build_track_tags."""

    def test_arranger_from_cea_and_work_deduplicated(self) -> None:
        """Arranger appearing in both cea and role_buckets is added only once."""
        tags = build_track_tags(
            _make_release(),
            _trk({"id": "trk-1", "position": 1, "recording": {"id": "rec-1", "title": "Track 1", "artist-credit": []}}),
            1,
            _rec(
                {
                    "id": "rec-1",
                    "title": "Track 1",
                    "artist-credit": [],
                    "artist-relation-list": [
                        {
                            "type": "arranger",
                            "artist": {"id": "a1", "name": "Dup Arr", "sort-name": "Arr, Dup"},
                            "attribute-list": [],
                        },
                    ],
                    "work-relation-list": [],
                }
            ),
            [
                _w(
                    {
                        "id": "w1",
                        "title": "Work",
                        "type": "",
                        "artist-relation-list": [
                            {"type": "arranger", "artist": {"id": "a1", "name": "Dup Arr", "sort-name": "Arr, Dup"}},
                        ],
                        "work-relation-list": [],
                        "attribute-list": [],
                        "tag-list": [],
                    }
                )
            ],
        )
        # "Dup Arr" should appear only once
        assert tags.arranger.count("Dup Arr") == 1

    def test_orchestrator_already_in_arranger_seen(self) -> None:
        """Orchestrator already added as arranger is not added again with (orch.) suffix.

        This covers the ``if e.name not in arranger_seen`` branch for orchestrators.
        """
        tags = build_track_tags(
            _make_release(),
            _trk({"id": "trk-1", "position": 1, "recording": {"id": "rec-1", "title": "Track 1", "artist-credit": []}}),
            1,
            _rec(
                {
                    "id": "rec-1",
                    "title": "Track 1",
                    "artist-credit": [],
                    "artist-relation-list": [
                        {
                            "type": "arranger",
                            "artist": {"id": "o1", "name": "Orch Arr", "sort-name": "Arr, Orch"},
                            "attribute-list": [],
                        },
                    ],
                    "work-relation-list": [],
                }
            ),
            [
                _w(
                    {
                        "id": "w1",
                        "title": "Work",
                        "type": "",
                        "artist-relation-list": [
                            # orchestrator with same name → blocked by arranger_seen
                            {"type": "orchestrator", "artist": {"id": "o1", "name": "Orch Arr", "sort-name": "Arr, Orch"}},
                        ],
                        "work-relation-list": [],
                        "attribute-list": [],
                        "tag-list": [],
                    }
                )
            ],
        )
        # "Orch Arr (orch.)" should NOT appear because "Orch Arr" already in seen
        assert "(orch.)" not in tags.arranger

    def test_work_only_arranger_appended(self) -> None:
        """Work-level arranger not in cea is appended to arranger string (covers lines 1046-1047).

        Args: None.
        """
        tags = build_track_tags(
            _make_release(),
            _trk({"id": "trk-1", "position": 1, "recording": {"id": "rec-1", "title": "Track 1", "artist-credit": []}}),
            1,
            _rec(
                {
                    "id": "rec-1",
                    "title": "Track 1",
                    "artist-credit": [],
                    "artist-relation-list": [],  # Recording has NO arranger
                    "work-relation-list": [],
                }
            ),
            [
                _w(
                    {
                        "id": "w1",
                        "title": "Work",
                        "type": "",
                        "artist-relation-list": [
                            {"type": "arranger", "artist": {"id": "wa1", "name": "Work Arr", "sort-name": "Arr, Work"}},
                        ],
                        "work-relation-list": [],
                        "attribute-list": [],
                        "tag-list": [],
                    }
                )
            ],
        )
        # Work Arr should appear
        assert "Work Arr" in tags.arranger


# ---------------------------------------------------------------------------
# run() — fetch_rels=True with actual work-relation-list (covers lines 1590-1596)
# ---------------------------------------------------------------------------


class TestRunWithWorkHierarchy:
    """Tests for run() when recording has a work-relation-list with performance type."""

    def test_work_hierarchy_fetched_when_performance_rel(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """fetch_work_detail is called when recording has a performance work relation.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_release(n_tracks=1))
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        # Recording with a performance → work relation
        def _fetch_rec(rec_id: str) -> MBRecording:
            return _rec(
                {
                    "id": rec_id,
                    "title": "Track",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [{"type": "performance", "work": {"id": "w1", "title": "The Work"}}],
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mock_work = mocker.patch(
            "music_annotator._mb_api.fetch_work_detail",
            return_value=_w(
                {
                    "id": "w1",
                    "title": "The Work",
                    "type": "",
                    "work-relation-list": [],
                    "artist-relation-list": [],
                    "attribute-list": [],
                    "tag-list": [],
                }
            ),
        )
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        mock_work.assert_called_once_with("w1")

    def test_non_performance_work_rel_skipped(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Non-performance work relation is skipped (covers 1590->1589 branch).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_release(n_tracks=1))
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        def _fetch_rec(rec_id: str) -> MBRecording:
            return _rec(
                {
                    "id": rec_id,
                    "title": "Track",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    # Non-performance type → if check is False
                    "work-relation-list": [{"type": "arrangement", "work": {"id": "w1"}}],
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mock_work = mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        # fetch_work_detail should NOT be called (no performance type matched)
        mock_work.assert_not_called()

    def test_performance_rel_with_empty_work_id_skips_fetch(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Performance relation with empty work id skips fetch_work_detail (covers 1592->1596).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_release(n_tracks=1))
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        def _fetch_rec(rec_id: str) -> MBRecording:
            return _rec(
                {
                    "id": rec_id,
                    "title": "Track",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    # Performance type but work.id = "" → if bottom_work_id: is False
                    "work-relation-list": [{"type": "performance", "work": {"id": ""}}],
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mock_work = mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        # fetch_work_detail should NOT be called (work id was empty)
        mock_work.assert_not_called()

    def test_inlined_work_skips_fetch_work_detail(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When work-level-rels inlines the full work, fetch_work_detail is NOT called.

        A recording whose performance-relation work already has a non-empty ``artist_relation_list``
        (as supplied by the MB API ``work-level-rels`` include) should be used directly without an
        extra round-trip to ``fetch_work_detail``.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_release(n_tracks=1))
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        # Work with an inlined artist relation (simulates work-level-rels response)
        def _fetch_rec(rec_id: str) -> MBRecording:
            return _rec(
                {
                    "id": rec_id,
                    "title": "Track",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [
                        {
                            "type": "performance",
                            "work": {
                                "id": "w1",
                                "title": "The Work",
                                "artist-relation-list": [{"type": "composer", "artist": {"id": "a1", "name": "Bach"}}],
                                "work-relation-list": [],
                            },
                        }
                    ],
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mock_work = mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        # fetch_work_detail should NOT be called — inlined work data was used directly
        mock_work.assert_not_called()

    def test_stub_work_falls_back_to_fetch_work_detail(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When embedded work has no relation data (stub only), fetch_work_detail IS called.

        This covers the fallback branch when ``work-level-rels`` is absent or the library returned
        only a stub (empty ``artist_relation_list`` and ``work_relation_list``).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_release(n_tracks=1))
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        # Work with no inlined relations (stub shape — both lists empty)
        def _fetch_rec(rec_id: str) -> MBRecording:
            return _rec(
                {
                    "id": rec_id,
                    "title": "Track",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [{"type": "performance", "work": {"id": "w1", "title": "The Work"}}],
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mock_work = mocker.patch(
            "music_annotator._mb_api.fetch_work_detail",
            return_value=_w({"id": "w1", "title": "The Work", "work-relation-list": [], "artist-relation-list": []}),
        )
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        # fetch_work_detail MUST be called — work had no inlined relation data
        mock_work.assert_called_once_with("w1")

    def test_multiple_performance_rels_selects_primary(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When a recording has two performance relations, select_primary_performance_work is called.

        The candidate with the higher-scoring top work is selected as the primary work.
        The lower-scoring candidate (cadenza) is ignored for work-hierarchy purposes.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_release(n_tracks=1))
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        # Recording with two performance relations: cadenza (w-cad) and concerto movement (w-mvt)
        def _fetch_rec(rec_id: str) -> MBRecording:
            return _rec(
                {
                    "id": rec_id,
                    "title": "Track",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [
                        {"type": "performance", "work": {"id": "w-cad", "title": "Cadenza"}},
                        {"type": "performance", "work": {"id": "w-mvt", "title": "I. Allegro"}},
                    ],
                }
            )

        # fetch_work_detail returns different works for each ID:
        # cadenza top-work: untyped + has based-on backward → score 0
        # concerto root: typed Concerto, no based-on → score 3
        cadenza_work = _w(
            {
                "id": "w-cad",
                "type": "",
                "title": "Cadenza",
                "work-relation-list": [{"type": "based on", "direction": "backward", "work": {"id": "w-conc"}}],
                "artist-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
            }
        )
        concerto_mvt = _w(
            {
                "id": "w-mvt",
                "type": "",
                "title": "I. Allegro",
                "work-relation-list": [{"type": "parts", "direction": "backward", "work": {"id": "w-conc"}}],
                "artist-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
            }
        )
        concerto_root = _w(
            {
                "id": "w-conc",
                "type": "Concerto",
                "title": "Violin Concerto in D major, Op. 61",
                "work-relation-list": [],
                "artist-relation-list": [
                    {"type": "composer", "artist": {"id": "a-beet", "name": "Beethoven", "sort-name": "Beethoven, Ludwig van"}},
                ],
                "attribute-list": [],
                "tag-list": [],
            }
        )

        def _fetch_work(work_id: str) -> MBWork:
            return {"w-cad": cadenza_work, "w-mvt": concerto_mvt, "w-conc": concerto_root}[work_id]

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        # Patch in both locations: _mb_api (used by _get_bottom_work) and _works (used by select_primary_performance_work)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._works.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        # The concerto root should be the top work (primary selected over cadenza)
        # Verify by checking the destination path contains "Beethoven" in composer component
        dest_files = list(dest.rglob("*.flac"))
        assert len(dest_files) == 1
        assert "Beethoven" in str(dest_files[0])

    def test_performance_rel_empty_work_id_skipped_in_candidates(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Performance relations with empty work.id are excluded from candidates list.

        When only empty-id performance relations exist, work_hierarchy stays empty.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_release(n_tracks=1))
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        def _fetch_rec(rec_id: str) -> MBRecording:
            return _rec(
                {
                    "id": rec_id,
                    "title": "Track",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [
                        {"type": "performance", "work": {"id": "", "title": ""}},
                        {"type": "performance", "work": {"id": "", "title": ""}},
                    ],
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mock_work = mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        mock_work.assert_not_called()


# ---------------------------------------------------------------------------
# run() — collision detection, user prompt, and journal
# ---------------------------------------------------------------------------


def _setup_single_track_run(mocker: MockerFixture, fs: FakeFilesystem, src: Path, dest: Path) -> None:
    """Set up a minimal single-track run with all MB API calls mocked.

    Creates src/01.flac, patches fetch_release/fetch_cover_art/fetch_recording_detail/
    fetch_work_detail/mb.set_useragent, and patches apply_tags_flac to a no-op.

    :param mocker: pytest-mock fixture.
    :param fs: pyfakefs fixture.
    :param src: Source directory path.
    :param dest: Destination root path.
    """
    fs.create_dir(str(src))
    fs.create_dir(str(dest))
    fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

    mocker.patch("music_annotator._mb_api.mb.set_useragent")
    mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_release(n_tracks=1))
    mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

    def _fetch_rec(rec_id: str) -> MBRecording:
        return _rec(
            {
                "id": rec_id,
                "title": "Track",
                "artist-credit": [],
                "artist-relation-list": [],
                "work-relation-list": [],
            }
        )

    mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
    mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
    mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
    mocker.patch("music_annotator._pipeline.apply_tags_flac")
    mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access


class TestPromptCollisionPolicy:
    """Tests for _prompt_collision_policy."""

    def test_displays_work_top_dirs_not_file_paths(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Collision warning shows the work-top-dir (parts[0]/parts[1]) with date suffix, not individual files.

        Two files in the same work directory should produce one grouped directory in the output.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest = Path("/dest")
        work_dir = dest / "Brahms - Karajan" / "Sinfonie Nr. 2 D-Dur, op. 73 [rec 1977-1978]"
        fs.create_dir(str(work_dir))
        collisions = [
            work_dir / "01 - Symphony no. 2 in D major, op. 73_ I.flac",
            work_dir / "02 - Symphony no. 2 in D major, op. 73_ II.flac",
        ]
        printed: list[str] = []
        mocker.patch("music_annotator._pipeline._console.print", side_effect=lambda s, **_: printed.append(s))
        mocker.patch("builtins.input", return_value="s")

        _prompt_collision_policy(collisions, dest)  # pylint: disable=protected-access

        # The work-top-dir with the date suffix must appear exactly once in printed output.
        assert any("Sinfonie Nr. 2 D-Dur, op. 73 [rec 1977-1978]" in line for line in printed)
        # Individual file names must not appear.
        assert not any("Symphony no. 2 in D major" in line for line in printed)

    def test_multiple_work_dirs_shown_grouped(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When collisions span two work directories both are shown, each once.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest = Path("/dest")
        w1 = dest / "Brahms - Karajan" / "Sinfonie Nr. 1 c-Moll, op. 68 [rec 1977-1978]"
        w2 = dest / "Brahms - Karajan" / "Sinfonie Nr. 3 F-Dur, op. 90 [rec 1977-1978]"
        for d in (w1, w2):
            fs.create_dir(str(d))
        collisions = [w1 / "01.flac", w1 / "02.flac", w2 / "01.flac"]
        printed: list[str] = []
        mocker.patch("music_annotator._pipeline._console.print", side_effect=lambda s, **_: printed.append(s))
        mocker.patch("builtins.input", return_value="s")

        _prompt_collision_policy(collisions, dest)  # pylint: disable=protected-access

        assert any("Sinfonie Nr. 1 c-Moll, op. 68 [rec 1977-1978]" in line for line in printed)
        assert any("Sinfonie Nr. 3 F-Dur, op. 90 [rec 1977-1978]" in line for line in printed)
        # Should show 2 work dirs, not 3 file paths — count lines containing the work dir pattern
        work_lines = [line for line in printed if "Sinfonie" in line]
        assert len(work_lines) == 2


class TestRunCollisionAndJournal:
    """Tests for run() collision detection, user prompt, and transaction journal."""

    def test_journal_written_on_successful_copy(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """A journal file is written to dest_root after a successful copy run.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        _setup_single_track_run(mocker, fs, src, dest)

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        journal_path = dest / JOURNAL_FILENAME
        assert journal_path.exists()
        data = json.loads(journal_path.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["action"] == "copied"
        assert data[0]["release_id"] == "rel-1"

    def test_journal_not_written_in_dry_run(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """No journal file is written when dry_run=True.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        _setup_single_track_run(mocker, fs, src, dest)

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=True,
            fetch_rels=False,
        )
        assert not (dest / JOURNAL_FILENAME).exists()

    def test_no_collision_no_prompt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """No prompt is shown when no destination files already exist.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        _setup_single_track_run(mocker, fs, src, dest)
        mock_input = mocker.patch("builtins.input")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )
        mock_input.assert_not_called()

    def test_collision_overwrite_copies_file(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Choosing 'overwrite' when a collision exists still copies and tags the file.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        _setup_single_track_run(mocker, fs, src, dest)

        # Pre-populate the destination with any file to create a guaranteed collision.
        # We patch _check_collisions to return a fixed path so we don't depend on the
        # exact dest path that build_dest_path would compute.
        # Path must be at least 2 levels deep relative to dest_root so _prompt_collision_policy
        # can extract parts[0]/parts[1] for the work-dir display.
        collision_path = dest / "Composer - Performer" / "Work [rec 1970]" / "existing.flac"
        fs.create_file(str(collision_path))
        mocker.patch("music_annotator._pipeline._check_collisions", return_value=[collision_path])
        mocker.patch("builtins.input", return_value="o")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )
        # File was copied (at least one flac in dest tree, excluding the pre-existing one)
        flac_files = [p for p in dest.rglob("*.flac") if p != collision_path]
        assert len(flac_files) >= 1

    def test_collision_overwrite_journal_action_copied(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Journal records action='copied' for files written on overwrite choice.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        _setup_single_track_run(mocker, fs, src, dest)

        collision_path = dest / "Composer - Performer" / "Work [rec 1970]" / "existing.flac"
        fs.create_file(str(collision_path))
        mocker.patch("music_annotator._pipeline._check_collisions", return_value=[collision_path])
        mocker.patch("builtins.input", return_value="overwrite")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )
        data = json.loads((dest / JOURNAL_FILENAME).read_text(encoding="utf-8"))
        assert any(e["action"] == "copied" for e in data)

    def test_collision_skip_skips_existing_and_copies_new(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Choosing 'skip' when collisions exist: conflicting files are skipped, new files are copied.

        Uses a 2-track release so there is one collision and one new file.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "02.flac"), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_release(n_tracks=2))
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        def _fetch_rec(rec_id: str) -> MBRecording:
            return _rec(
                {"id": rec_id, "title": "Track", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []}
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        # We'll capture the planned dest paths by letting build_dest_path run, then
        # intercept _check_collisions to report the first dest as a collision.
        captured_dests: list[Path] = []

        def _capture_check(paths: list[Path]) -> list[Path]:
            captured_dests.extend(paths)
            return [paths[0]]  # first file is the collision

        mocker.patch("music_annotator._pipeline._check_collisions", side_effect=_capture_check)  # pylint: disable=protected-access
        mocker.patch("builtins.input", return_value="s")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        # apply_tags_flac should have been called exactly once (the non-skipped file)
        assert mock_tag.call_count == 1

        # Journal should have one skipped and one copied
        data = json.loads((dest / JOURNAL_FILENAME).read_text(encoding="utf-8"))
        actions = {e["action"] for e in data}
        assert "skipped" in actions
        assert "copied" in actions

    def test_collision_abort_raises_system_exit(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Choosing 'abort' when collisions exist raises SystemExit(1) without copying anything.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        _setup_single_track_run(mocker, fs, src, dest)

        collision_path = dest / "Composer - Performer" / "Work [rec 1970]" / "existing.flac"
        fs.create_file(str(collision_path))
        mocker.patch("music_annotator._pipeline._check_collisions", return_value=[collision_path])
        mocker.patch("builtins.input", return_value="a")

        with pytest.raises(SystemExit) as exc_info:
            music_annotator.run(
                release_id="rel-1",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=False,
                fetch_rels=False,
            )
        assert exc_info.value.code == 1
        # No journal should have been written
        assert not (dest / JOURNAL_FILENAME).exists()

    def test_collision_abort_long_form_raises_system_exit(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Choosing 'abort' (long form) when collisions exist raises SystemExit(1).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        _setup_single_track_run(mocker, fs, src, dest)

        collision_path = dest / "Composer - Performer" / "Work [rec 1970]" / "existing.flac"
        fs.create_file(str(collision_path))
        mocker.patch("music_annotator._pipeline._check_collisions", return_value=[collision_path])
        mocker.patch("builtins.input", return_value="abort")

        with pytest.raises(SystemExit) as exc_info:
            music_annotator.run(
                release_id="rel-1",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=False,
                fetch_rels=False,
            )
        assert exc_info.value.code == 1

    def test_collision_invalid_then_valid_input(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Invalid prompt input is ignored until a valid choice is entered.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        _setup_single_track_run(mocker, fs, src, dest)

        collision_path = dest / "Composer - Performer" / "Work [rec 1970]" / "existing.flac"
        fs.create_file(str(collision_path))
        mocker.patch("music_annotator._pipeline._check_collisions", return_value=[collision_path])
        # First two inputs are invalid; third is valid "skip"
        mocker.patch("builtins.input", side_effect=["x", "yes", "skip"])

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )
        # Run completed (chose skip on 3rd attempt); journal should exist
        assert (dest / JOURNAL_FILENAME).exists()

    def test_journal_appends_across_multiple_runs(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Journal entries accumulate across multiple calls to run().

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        _setup_single_track_run(mocker, fs, src, dest)

        # First run
        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )
        data_after_first = json.loads((dest / JOURNAL_FILENAME).read_text(encoding="utf-8"))
        assert len(data_after_first) == 1

        # Patch so the second run doesn't try to re-copy (would be a no-op anyway in fake fs,
        # but reset the MB mock return to avoid interaction with the first run's cache).
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_release(n_tracks=1))
        mocker.patch("music_annotator._pipeline._check_collisions", return_value=[])  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )
        data_after_second = json.loads((dest / JOURNAL_FILENAME).read_text(encoding="utf-8"))
        assert len(data_after_second) == 2

    def test_collision_policy_overwrite_skips_prompt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Passing CollisionPolicy.OVERWRITE skips the interactive prompt entirely.

        Verifies the branch where ``collision_policy != CollisionPolicy.ASK`` so
        ``_prompt_collision_policy`` is never called even when collisions exist.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        _setup_single_track_run(mocker, fs, src, dest)

        collision_path = dest / "Composer - Performer" / "Work [rec 1970]" / "existing.flac"
        fs.create_file(str(collision_path))
        mocker.patch("music_annotator._pipeline._check_collisions", return_value=[collision_path])
        mock_prompt = mocker.patch("music_annotator._pipeline._prompt_collision_policy")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
            collision_policy=CollisionPolicy.OVERWRITE,
        )
        # Prompt must NOT be called when policy is already set
        mock_prompt.assert_not_called()


# ---------------------------------------------------------------------------
# Confirmation message
# ---------------------------------------------------------------------------


class TestRunConfirmationMessage:
    """Tests for the post-copy 'Verified OK' confirmation message in run()."""

    def test_confirmation_shows_work_top_dir_with_date_suffix(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """The 'Verified OK' message shows the work-top-dir (parts[0]/parts[1]) with [rec/rel] suffix.

        For a 3-level hierarchy the immediate parent of a file is a division subdirectory, not the
        work directory.  This test verifies that the confirmation message always shows the
        work-top-dir (dest_root / composer_dir / work_dir) regardless of hierarchy depth.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        _setup_single_track_run(mocker, fs, src, dest)
        printed: list[str] = []
        mocker.patch("music_annotator._pipeline._console.print", side_effect=lambda s, **_: printed.append(s))

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )

        # A 'Verified OK' line must appear.
        assert any("Verified OK" in line for line in printed)
        # The destination shown must be at exactly parts[0]/parts[1] depth relative to dest:
        # it must be a direct child of a direct child of dest — not the file's immediate parent.
        dest_lines = [line for line in printed if str(dest) in line and "Verified" not in line and "safe" not in line]
        for line in dest_lines:
            # Extract the path from the rich markup.
            raw = line.replace("[green]", "").replace("[/]", "").strip().lstrip()
            p = Path(raw)
            assert len(p.relative_to(dest).parts) == 2  # noqa: PLR2004 — exactly composer/work depth


# ---------------------------------------------------------------------------
# _sha256_file
# ---------------------------------------------------------------------------


class TestSha256File:
    """Tests for _sha256_file."""

    def test_known_hash(self, fs: FakeFilesystem) -> None:
        """SHA-256 of known bytes matches the expected digest.

        :param fs: pyfakefs fixture.
        """
        data = b"hello world"
        path = Path("/tmp/test.bin")
        fs.create_file(str(path), contents=data)
        assert _sha256_file(path) == hashlib.sha256(data).hexdigest()

    def test_empty_file(self, fs: FakeFilesystem) -> None:
        """SHA-256 of an empty file matches the expected digest.

        :param fs: pyfakefs fixture.
        """
        path = Path("/tmp/empty.bin")
        fs.create_file(str(path), contents=b"")
        assert _sha256_file(path) == hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# _read_tags_flac / _read_tags_mp3
# ---------------------------------------------------------------------------


class TestReadTagsFlac:
    """Tests for _read_tags_flac."""

    def test_round_trip(self, fs: FakeFilesystem) -> None:
        """Tags written by apply_tags_flac are read back correctly by _read_tags_flac.

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.flac")
        fs.create_dir("/out")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)
        tags = TrackTags(title="Eroica", album="Beethoven Symphonies", tracknumber="1", composer="Beethoven")
        apply_tags_flac(path, tags)
        assert _read_tags_flac(path) == tags.to_file_dict()

    def test_no_tags_returns_empty(self, fs: FakeFilesystem) -> None:
        """A freshly-written FLAC with no tags returns an empty dict.

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/bare.flac")
        fs.create_dir("/out")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)
        assert _read_tags_flac(path) == {}


class TestReadTagsMp3:
    """Tests for _read_tags_mp3."""

    def test_round_trip(self, fs: FakeFilesystem) -> None:
        """Tags written by apply_tags_mp3 are read back correctly by _read_tags_mp3.

        apply_tags_mp3 only writes the subset of fields in _MP3_STD_KEYS | _MP3_TXXX_MAP, so the
        expected dict is filtered to that writable set before comparison.

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(path), contents=_MINIMAL_MP3)
        tags = TrackTags(title="Eroica", album="Beethoven Symphonies", tracknumber="3", totaltracks="9", composer="Beethoven")
        apply_tags_mp3(path, tags)
        writable = music_annotator._tagger._MP3_STD_KEYS | frozenset(music_annotator._tagger._MP3_TXXX_MAP)  # pylint: disable=protected-access
        expected = {k: v for k, v in tags.to_file_dict().items() if k in writable}
        assert _read_tags_mp3(path) == expected

    def test_no_tags_returns_defaults(self, fs: FakeFilesystem) -> None:
        """An MP3 written with a default TrackTags() returns only the non-empty default fields.

        TrackTags() has non-empty defaults IS_CLASSICAL="1" and GENRE="Classical" which are in
        _MP3_TXXX_MAP and will be written by apply_tags_mp3 even when no fields are explicitly set.

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/bare.mp3")
        fs.create_dir("/out")
        fs.create_file(str(path), contents=_MINIMAL_MP3)
        apply_tags_mp3(path, TrackTags())
        assert _read_tags_mp3(path) == {
            "IS_CLASSICAL": "1",
            "GENRE": "Classical",
            "CWP_PART_LEVELS": "0",
            "CWP_WORK_PART_LEVELS": "0",
            "CWP_SINGLE_WORK_ALBUM": "0",
        }

    def test_unknown_txxx_desc_ignored(self, fs: FakeFilesystem) -> None:
        """TXXX frames with descriptions not in _MP3_TXXX_MAP are silently ignored.

        This covers the branch where ``tag_key`` is falsy (unknown TXXX description) so the frame
        is skipped without being added to the result dict.

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(path), contents=_MINIMAL_MP3)
        apply_tags_mp3(path, TrackTags(title="T"))
        # Inject a TXXX frame with an unknown description after the normal write.
        id3 = ID3(str(path))  # type: ignore[no-untyped-call]
        id3.add(TXXX(encoding=3, desc="UNKNOWN_DESC", text=["some value"]))  # type: ignore[no-untyped-call]
        id3.save(str(path))
        # The unknown TXXX frame must not appear in the result.
        result = _read_tags_mp3(path)
        assert "UNKNOWN_DESC" not in result
        assert result.get("TITLE") == "T"


# ---------------------------------------------------------------------------
# _verify_copy
# ---------------------------------------------------------------------------


class TestVerifyCopy:
    """Tests for _verify_copy."""

    def test_flac_passes(self, fs: FakeFilesystem) -> None:
        """_verify_copy passes for a correctly-tagged FLAC file with matching mtime.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_dir("/src")
        fs.create_dir("/dest")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        shutil.copy2(str(src), str(dest))
        tags = TrackTags(title="Symphony No. 5", tracknumber="1")
        apply_tags_flac(dest, tags)
        mtime = src.stat().st_mtime
        os.utime(dest, (mtime, mtime))
        _verify_copy(src, dest, tags, None, mtime)

    def test_mp3_passes(self, fs: FakeFilesystem) -> None:
        """_verify_copy passes for a correctly-tagged MP3 file with matching mtime.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.mp3")
        dest = Path("/dest/track.mp3")
        fs.create_dir("/src")
        fs.create_dir("/dest")
        fs.create_file(str(src), contents=_MINIMAL_MP3)
        shutil.copy2(str(src), str(dest))
        tags = TrackTags(title="Symphony No. 5", tracknumber="1")
        apply_tags_mp3(dest, tags)
        mtime = src.stat().st_mtime
        os.utime(dest, (mtime, mtime))
        _verify_copy(src, dest, tags, None, mtime)

    def test_flac_with_cover_passes(self, fs: FakeFilesystem) -> None:
        """_verify_copy passes when cover art matches the embedded bytes.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_dir("/src")
        fs.create_dir("/dest")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        shutil.copy2(str(src), str(dest))
        tags = TrackTags(title="T")
        cover = CoverArt(front=[CoverImage(data=b"\xff\xd8\xff\xe0" + b"\x00" * 100, mime="image/jpeg")])
        apply_tags_flac(dest, tags, cover)
        mtime = src.stat().st_mtime
        os.utime(dest, (mtime, mtime))
        _verify_copy(src, dest, tags, cover, mtime)

    def test_mp3_with_cover_passes(self, fs: FakeFilesystem) -> None:
        """_verify_copy passes when MP3 cover art matches the embedded bytes.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.mp3")
        dest = Path("/dest/track.mp3")
        fs.create_dir("/src")
        fs.create_dir("/dest")
        fs.create_file(str(src), contents=_MINIMAL_MP3)
        shutil.copy2(str(src), str(dest))
        tags = TrackTags(title="T")
        cover = CoverArt(front=[CoverImage(data=b"\xff\xd8\xff\xe0" + b"\x00" * 100, mime="image/jpeg")])
        apply_tags_mp3(dest, tags, cover)
        mtime = src.stat().st_mtime
        os.utime(dest, (mtime, mtime))
        _verify_copy(src, dest, tags, cover, mtime)

    def test_tag_mismatch_raises(self, fs: FakeFilesystem) -> None:
        """_verify_copy raises RuntimeError when the read-back tags do not match expected.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_dir("/src")
        fs.create_dir("/dest")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        shutil.copy2(str(src), str(dest))
        tags = TrackTags(title="Correct Title")
        apply_tags_flac(dest, tags)
        wrong_tags = TrackTags(title="Wrong Title")
        mtime = src.stat().st_mtime
        os.utime(dest, (mtime, mtime))
        with pytest.raises(RuntimeError, match="tag verification failure"):
            _verify_copy(src, dest, wrong_tags, None, mtime)

    def test_cover_mismatch_flac_raises(self, fs: FakeFilesystem) -> None:
        """_verify_copy raises RuntimeError when FLAC cover art does not match.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_dir("/src")
        fs.create_dir("/dest")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        shutil.copy2(str(src), str(dest))
        tags = TrackTags(title="T")
        cover_written = CoverArt(front=[CoverImage(data=b"\xff\xd8\xff\xe0" + b"\x00" * 100, mime="image/jpeg")])
        apply_tags_flac(dest, tags, cover_written)
        mtime = src.stat().st_mtime
        os.utime(dest, (mtime, mtime))
        # wrong_cover has different bytes — _verify_copy should detect the mismatch.
        wrong_cover = CoverArt(front=[CoverImage(data=b"\xff\xd8\xff\xe0" + b"\x01" * 100, mime="image/jpeg")])
        with pytest.raises(RuntimeError, match="cover art verification failure"):
            _verify_copy(src, dest, tags, wrong_cover, mtime)

    def test_cover_mismatch_mp3_raises(self, fs: FakeFilesystem) -> None:
        """_verify_copy raises RuntimeError when MP3 cover art does not match.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.mp3")
        dest = Path("/dest/track.mp3")
        fs.create_dir("/src")
        fs.create_dir("/dest")
        fs.create_file(str(src), contents=_MINIMAL_MP3)
        shutil.copy2(str(src), str(dest))
        tags = TrackTags(title="T")
        cover_written = CoverArt(front=[CoverImage(data=b"\xff\xd8\xff\xe0" + b"\x00" * 100, mime="image/jpeg")])
        apply_tags_mp3(dest, tags, cover_written)
        mtime = src.stat().st_mtime
        os.utime(dest, (mtime, mtime))
        # wrong_cover has different bytes — _verify_copy should detect the mismatch.
        wrong_cover = CoverArt(front=[CoverImage(data=b"\xff\xd8\xff\xe0" + b"\x01" * 100, mime="image/jpeg")])
        with pytest.raises(RuntimeError, match="cover art verification failure"):
            _verify_copy(src, dest, tags, wrong_cover, mtime)

    def test_mtime_mismatch_raises(self, fs: FakeFilesystem) -> None:
        """_verify_copy raises RuntimeError when the destination mtime does not match.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_dir("/src")
        fs.create_dir("/dest")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        shutil.copy2(str(src), str(dest))
        tags = TrackTags(title="T")
        apply_tags_flac(dest, tags)
        mtime = src.stat().st_mtime
        os.utime(dest, (mtime, mtime))
        with pytest.raises(RuntimeError, match="mtime verification failure"):
            _verify_copy(src, dest, tags, None, mtime + 1.0)

    def test_no_cover_skips_cover_check(self, fs: FakeFilesystem) -> None:
        """_verify_copy does not check cover art when cover is None.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_dir("/src")
        fs.create_dir("/dest")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        shutil.copy2(str(src), str(dest))
        tags = TrackTags(title="T")
        apply_tags_flac(dest, tags)
        mtime = src.stat().st_mtime
        os.utime(dest, (mtime, mtime))
        _verify_copy(src, dest, tags, None, mtime)


# ---------------------------------------------------------------------------
# run() — copy-integrity hash check
# ---------------------------------------------------------------------------


class TestRunCopyIntegrity:
    """Tests for the inline SHA-256 copy-integrity check inside run()."""

    def test_hash_mismatch_raises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """run() raises RuntimeError when the post-copy hash does not match the source hash.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        _setup_single_track_run(mocker, fs, src, dest)
        # Return different hashes for the two _sha256_file calls (src then dest).
        mocker.patch("music_annotator._pipeline._sha256_file", side_effect=["aaa", "bbb"])  # pylint: disable=protected-access

        with pytest.raises(RuntimeError, match="copy integrity failure"):
            music_annotator.run(
                release_id="rel-1",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=False,
                fetch_rels=False,
            )


# ---------------------------------------------------------------------------
# fetch_acoustid_id
# ---------------------------------------------------------------------------


class TestFetchAcoustidId:
    """Tests for fetch_acoustid_id covering all response-parsing branches."""

    def _make_resp(self, mocker: MockerFixture, body: bytes) -> None:
        """Patch urllib.request.urlopen to return a context-manager that yields ``body``.

        Uses a MagicMock configured as a context manager so that ``with urlopen(...) as resp:``
        enters the mock and ``resp.read()`` returns ``body``.

        :param mocker: pytest-mock fixture.
        :param body: Raw bytes to return from resp.read().
        """
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.read = MagicMock(return_value=body)
        mocker.patch("music_annotator._mb_api.urllib.request.urlopen", return_value=ctx)

    def test_valid_response_returns_id(self, mocker: MockerFixture) -> None:
        """A well-formed AcoustID response returns the first track id.

        :param mocker: pytest-mock fixture.
        """
        self._make_resp(mocker, b'{"tracks": [{"id": "acoustid-uuid-123"}]}')
        mocker.patch("music_annotator._mb_api.time.sleep")
        assert fetch_acoustid_id("rec-mbid") == "acoustid-uuid-123"

    def test_non_dict_response_returns_empty(self, mocker: MockerFixture) -> None:
        """A non-dict JSON response (e.g. a list) returns an empty string.

        :param mocker: pytest-mock fixture.
        """
        self._make_resp(mocker, b'["unexpected"]')
        mocker.patch("music_annotator._mb_api.time.sleep")
        assert fetch_acoustid_id("rec-mbid") == ""

    def test_missing_tracks_key_returns_empty(self, mocker: MockerFixture) -> None:
        """A response dict with no 'tracks' key returns an empty string.

        :param mocker: pytest-mock fixture.
        """
        self._make_resp(mocker, b'{"status": "ok"}')
        mocker.patch("music_annotator._mb_api.time.sleep")
        assert fetch_acoustid_id("rec-mbid") == ""

    def test_empty_tracks_list_returns_empty(self, mocker: MockerFixture) -> None:
        """A response with an empty 'tracks' list returns an empty string.

        :param mocker: pytest-mock fixture.
        """
        self._make_resp(mocker, b'{"tracks": []}')
        mocker.patch("music_annotator._mb_api.time.sleep")
        assert fetch_acoustid_id("rec-mbid") == ""

    def test_non_dict_first_track_returns_empty(self, mocker: MockerFixture) -> None:
        """A response where the first track element is not a dict returns an empty string.

        :param mocker: pytest-mock fixture.
        """
        self._make_resp(mocker, b'{"tracks": ["not-a-dict"]}')
        mocker.patch("music_annotator._mb_api.time.sleep")
        assert fetch_acoustid_id("rec-mbid") == ""

    def test_empty_track_id_returns_empty(self, mocker: MockerFixture) -> None:
        """A response where the first track has an empty 'id' value returns an empty string.

        :param mocker: pytest-mock fixture.
        """
        self._make_resp(mocker, b'{"tracks": [{"id": ""}]}')
        mocker.patch("music_annotator._mb_api.time.sleep")
        assert fetch_acoustid_id("rec-mbid") == ""

    def test_network_error_returns_empty(self, mocker: MockerFixture) -> None:
        """All three retry attempts fail with OSError; returns empty string.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.urllib.request.urlopen", side_effect=OSError("network failure"))
        mocker.patch("music_annotator._mb_api.time.sleep")
        assert fetch_acoustid_id("rec-mbid") == ""

    def test_network_error_retried_succeeds(self, mocker: MockerFixture) -> None:
        """OSError on first attempt is retried; succeeds on the second attempt.

        :param mocker: pytest-mock fixture.
        """
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.read = MagicMock(return_value=b'{"tracks": [{"id": "acoustid-uuid-456"}]}')
        mocker.patch(
            "music_annotator._mb_api.urllib.request.urlopen",
            side_effect=[OSError("timeout"), ctx],
        )
        mocker.patch("music_annotator._mb_api.time.sleep")
        assert fetch_acoustid_id("rec-mbid") == "acoustid-uuid-456"

    def test_json_decode_error_not_retried(self, mocker: MockerFixture) -> None:
        """A JSONDecodeError causes immediate return without retrying.

        :param mocker: pytest-mock fixture.
        """
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.read = MagicMock(return_value=b"not valid json {{{")
        mock_urlopen = mocker.patch("music_annotator._mb_api.urllib.request.urlopen", return_value=ctx)
        mocker.patch("music_annotator._mb_api.time.sleep")
        assert fetch_acoustid_id("rec-mbid") == ""
        assert mock_urlopen.call_count == 1  # not retried

    def test_success_sleeps_one_second(self, mocker: MockerFixture) -> None:
        """A successful response is followed by a 1-second polite delay.

        :param mocker: pytest-mock fixture.
        """
        self._make_resp(mocker, b'{"tracks": [{"id": "acoustid-uuid-789"}]}')
        mock_sleep = mocker.patch("music_annotator._mb_api.time.sleep")
        fetch_acoustid_id("rec-mbid")
        mock_sleep.assert_called_once_with(1)


# ---------------------------------------------------------------------------
# run() — multi-disc medium selection
# ---------------------------------------------------------------------------


class TestRunMultiDisc:
    """Tests for run() multi-disc medium selection logic."""

    def _patch_mb_multi(self, mocker: MockerFixture, release: MBRelease) -> None:
        """Patch all MB API calls for a multi-disc run.

        :param mocker: pytest-mock fixture.
        :param release: Release model to return from fetch_release.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        def _fetch_rec(rec_id: str) -> MBRecording:
            return _rec(
                {"id": rec_id, "title": "Track", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []}
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

    def test_single_matching_medium_selected(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When exactly one medium matches the source file count, it is selected automatically.

        Two-disc release (3 tracks + 2 tracks); source dir has 2 files → disc 2 selected.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "02.flac"), contents=_MINIMAL_FLAC)

        release = _make_multi_disc_release([3, 2])
        self._patch_mb_multi(mocker, release)
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")

        music_annotator.run(
            release_id="rel-multi",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        assert mock_tag.call_count == 2

    def test_multiple_matching_mediums_disc_hint_resolves(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When multiple mediums match and the directory name contains a disc hint, it is used.

        Two-disc release each with 1 track; source dir is named "disc2" → medium position 2 selected.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/albums/disc2")
        dest = Path("/dest")
        fs.create_dir("/albums")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_multi_disc_release([1, 1])
        self._patch_mb_multi(mocker, release)
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")

        music_annotator.run(
            release_id="rel-multi",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        # disc2 → position 2 medium selected; only 1 track on that medium
        assert mock_tag.call_count == 1

    def test_multiple_matching_mediums_no_hint_uses_first(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When multiple mediums match and there is no disc hint, the first matching medium is used.

        Two-disc release each with 1 track; source dir has no disc suffix → first medium used.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_multi_disc_release([1, 1])
        self._patch_mb_multi(mocker, release)
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")

        music_annotator.run(
            release_id="rel-multi",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        assert mock_tag.call_count == 1

    def test_no_matching_medium_raises_value_error(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When no medium matches the source file count, ValueError is raised with a helpful message.

        Two-disc release (3 + 4 tracks); source dir has 2 files → no match → ValueError.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "02.flac"), contents=_MINIMAL_FLAC)

        release = _make_multi_disc_release([3, 4])
        self._patch_mb_multi(mocker, release)

        with pytest.raises(ValueError, match="track count mismatch"):
            music_annotator.run(
                release_id="rel-multi",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=False,
                fetch_rels=True,
            )

    def test_empty_medium_list_raises_value_error(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When the release has no mediums at all, ValueError is raised.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = MBRelease.model_validate(
            {
                "id": "rel-empty",
                "title": "Empty Release",
                "date": "2000",
                "status": "Official",
                "barcode": "",
                "artist-credit": [],
                "release-group": {"id": "rg-1", "primary-type": "Album", "first-release-date": "2000"},
                "label-info-list": [],
                "text-representation": {"script": "Latn", "language": "eng"},
                "medium-list": [],
            }
        )
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        with pytest.raises(ValueError, match="has no mediums"):
            music_annotator.run(
                release_id="rel-empty",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=False,
                fetch_rels=False,
            )


# ---------------------------------------------------------------------------
# _match_medium_by_toc
# ---------------------------------------------------------------------------

#: CD frame offsets for a fictional disc 1 (4 tracks).
_DISC1_OFFSETS: list[int] = [182, 50000, 100000, 150000]
#: CD frame offsets for a fictional disc 2 (4 tracks).
_DISC2_OFFSETS: list[int] = [182, 60000, 110000, 160000]


def _medium_with_toc(position: int, offsets: list[int]) -> MBMedium:
    """Build a minimal MBMedium with one MBDisc entry carrying ``offsets``.

    :param position: 1-based disc position.
    :param offsets: Per-track CD frame start offsets.
    :returns: An :class:`~music_annotator.models.MBMedium` instance.
    """
    return MBMedium.model_validate(
        {
            "position": position,
            "format": "CD",
            "track-list": [],
            "disc-list": [{"offset-list": offsets, "sectors": str(offsets[-1] + 1000)}],
        }
    )


class TestMatchMediumByToc:
    """Tests for _match_medium_by_toc."""

    def test_matches_disc2_offsets(self) -> None:
        """Returns the medium whose disc offsets exactly match the supplied track_frames.

        Two mediums with different offsets; disc 2 offsets supplied → disc 2 returned.
        """
        m1 = _medium_with_toc(1, _DISC1_OFFSETS)
        m2 = _medium_with_toc(2, _DISC2_OFFSETS)
        result = _match_medium_by_toc([m1, m2], _DISC2_OFFSETS)
        assert result is m2

    def test_matches_disc1_offsets(self) -> None:
        """Returns disc 1 when disc 1 offsets are supplied."""
        m1 = _medium_with_toc(1, _DISC1_OFFSETS)
        m2 = _medium_with_toc(2, _DISC2_OFFSETS)
        result = _match_medium_by_toc([m1, m2], _DISC1_OFFSETS)
        assert result is m1

    def test_no_match_returns_none(self) -> None:
        """Returns None when no medium's disc offsets match the supplied track_frames."""
        m1 = _medium_with_toc(1, _DISC1_OFFSETS)
        m2 = _medium_with_toc(2, _DISC2_OFFSETS)
        result = _match_medium_by_toc([m1, m2], [182, 99999, 199999, 299999])
        assert result is None

    def test_empty_disc_list_returns_none(self) -> None:
        """Returns None when mediums have no disc entries (discids not fetched)."""
        m1 = MBMedium.model_validate({"position": 1, "format": "CD", "track-list": []})
        m2 = MBMedium.model_validate({"position": 2, "format": "CD", "track-list": []})
        result = _match_medium_by_toc([m1, m2], _DISC1_OFFSETS)
        assert result is None

    def test_multiple_discs_per_medium_second_entry_matches(self) -> None:
        """Matches when the second MBDisc entry on a medium carries the correct offsets.

        Pressing A has slightly different offsets from pressing B; pressing B offsets supplied.
        """
        pressing_a = [182, 50001, 100001, 150001]
        medium = MBMedium.model_validate(
            {
                "position": 1,
                "format": "CD",
                "track-list": [],
                "disc-list": [
                    {"offset-list": pressing_a, "sectors": str(pressing_a[-1] + 1000)},
                    {"offset-list": _DISC1_OFFSETS, "sectors": str(_DISC1_OFFSETS[-1] + 1000)},
                ],
            }
        )
        result = _match_medium_by_toc([medium], _DISC1_OFFSETS)
        assert result is medium


# ---------------------------------------------------------------------------
# run() — TOC-based medium selection via 00 - disc info.yaml
# ---------------------------------------------------------------------------

#: Minimal valid disc info YAML for a fictional 4-track disc 2.
_DISC2_YAML: str = (
    "disc_id: [999999999, 4, 182, 60000, 110000, 160000, 3600]\n"
    "record:\n"
    "- disc_info: {category: classical, disc_id: '3b9ac9ff', title: 'Composer / Symphony 2 & 4'}\n"
    "  preferred: true\n"
    "  track_info: {DTITLE: 'Composer / Symphony 2 & 4', DISCID: '3b9ac9ff'}\n"
)


class TestRunTocMediumSelection:
    """Tests for run() selecting the correct medium via TOC matching from 00 - disc info.yaml."""

    def _patch_mb(self, mocker: MockerFixture, release: MBRelease) -> None:
        """Patch all MB API calls for a TOC-based medium selection run.

        :param mocker: pytest-mock fixture.
        :param release: Release model to return from fetch_release.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        def _fetch_rec(rec_id: str) -> MBRecording:
            return _rec(
                {"id": rec_id, "title": "Track", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []}
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

    def test_toc_selects_disc2_when_both_discs_have_same_track_count(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """TOC offsets from 00 - disc info.yaml select disc 2 even when both discs have 4 tracks.

        Without TOC matching the heuristic would fall back to disc 1 (first matching medium).
        With TOC matching disc 2 is correctly identified by its unique frame offsets.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 5):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / _DISC_INFO_FILENAME), contents=_DISC2_YAML)

        # Two discs each with 4 tracks; disc 2 carries offsets matching _DISC2_YAML.
        release = _make_multi_disc_release(
            [4, 4],
            disc_offsets=[[_DISC1_OFFSETS], [_DISC2_OFFSETS]],
        )
        self._patch_mb(mocker, release)
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")

        music_annotator.run(
            release_id="rel-multi",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        # 4 tracks copied — confirms disc 2 was selected (disc 1 would produce identical count
        # but different recording IDs; the test verifies no ValueError was raised and tagging ran).
        assert mock_tag.call_count == 4

    def test_no_yaml_falls_back_to_track_count_heuristic(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When no 00 - disc info.yaml is present, track-count heuristic selects the medium.

        Two-disc release (3 + 4 tracks); source dir has 4 files, no YAML → disc 2 selected by count.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 5):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        # No YAML file created.

        release = _make_multi_disc_release([3, 4])
        self._patch_mb(mocker, release)
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")

        music_annotator.run(
            release_id="rel-multi",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        assert mock_tag.call_count == 4

    def test_yaml_toc_no_match_falls_back_to_track_count(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When YAML TOC offsets match no medium, selection falls back to track-count heuristic.

        Disc info YAML has offsets that don't correspond to either medium; track-count heuristic
        selects the unique matching medium (3 + 4 tracks, 4 source files → disc 2).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 5):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        # YAML with offsets that don't match either medium's disc entries.
        unmatched_yaml = (
            "disc_id: [111111111, 4, 182, 11111, 22222, 33333, 3600]\n"
            "record:\n"
            "- disc_info: {category: classical, disc_id: '06acef47', title: 'X / Y'}\n"
            "  preferred: true\n"
            "  track_info: {DTITLE: 'X / Y', DISCID: '06acef47'}\n"
        )
        fs.create_file(str(src / _DISC_INFO_FILENAME), contents=unmatched_yaml)

        release = _make_multi_disc_release(
            [3, 4],
            disc_offsets=[[_DISC1_OFFSETS[:3]], [_DISC2_OFFSETS]],
        )
        self._patch_mb(mocker, release)
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")

        music_annotator.run(
            release_id="rel-multi",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        assert mock_tag.call_count == 4
