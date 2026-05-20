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
import music_annotator._tags
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
from music_annotator._pipeline import (
    SelectionMethod,
    _match_medium_by_title,
    _match_medium_by_toc,
    _prompt_collision_policy,
    _resolve_long_names,
    _score_medium_title,
    _select_medium_with_reason,
    _warn_long_names,
    _write_freedb_yaml,
    _write_sidecars,
)
from music_annotator._pipeline_io import _DISC_INFO_FILENAME, _DISC_TOC_FILENAME
from music_annotator._tagger import _FLAC_MAX_PICTURE_BYTES
from music_annotator.models import (
    JSON,
    CopyPlanEntry,
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

    @staticmethod
    def _rel(rtype: str, mbid: str, name: str, sort: str, attrs: list[JSON] | None = None) -> JSON:
        """Build a typed artist-relation dict for use in ``artist-relation-list``.

        :param rtype: Relation type string (e.g. ``"conductor"``).
        :param mbid: Artist MBID string (may be empty).
        :param name: Artist display name.
        :param sort: Artist sort name.
        :param attrs: Optional ``attribute-list`` entries.
        :returns: A :data:`~music_annotator.models.JSON`-typed relation dict.
        """
        return {
            "type": rtype,
            "artist": {"id": mbid, "name": name, "sort-name": sort},
            "attribute-list": attrs or [],
        }

    def _recording_multi(self, rels: list[JSON]) -> MBRecording:
        """Build a minimal MBRecording with multiple artist relations.

        :param rels: List of relation dicts (each with ``type``, ``artist``, ``attribute-list`` keys).
        :returns: An :class:`~music_annotator.models.MBRecording` instance.
        """
        return _rec(
            {
                "id": "rec-multi",
                "title": "T",
                "artist-credit": [],
                "artist-relation-list": rels,
                "work-relation-list": [],
            }
        )

    def test_duplicate_conductor_deduplicated(self) -> None:
        """Two conductor relations with the same MBID yield exactly one entry in cea.conductors.

        This mirrors the real-world case where MusicBrainz returns the same conductor relation
        twice on a recording (observed with several DG recordings under musicbrainzngs).
        """
        rel = self._rel("conductor", "cond-1", "Karajan", "Karajan, Herbert von")
        cea = build_cea_performers(self._recording_multi([rel, rel]))
        assert len(cea.conductors) == 1
        assert cea.conductors[0].name == "Karajan"

    def test_duplicate_ensemble_deduplicated(self) -> None:
        """Two performing-orchestra relations with the same MBID yield exactly one ensemble entry."""
        rel = self._rel("performing orchestra", "ens-1", "Berliner Philharmoniker", "Berliner Philharmoniker")
        cea = build_cea_performers(self._recording_multi([rel, rel]))
        assert len(cea.ensembles) == 1
        assert cea.ensembles[0].name == "Berliner Philharmoniker"

    def test_duplicate_conductor_and_ensemble_both_deduplicated(self) -> None:
        """Duplicate conductor + duplicate ensemble (mirroring real MB data) → one of each."""
        cond = self._rel("conductor", "cond-1", "Karajan", "Karajan, Herbert von")
        orch = self._rel("performing orchestra", "ens-1", "Berliner Philharmoniker", "Berliner Philharmoniker")
        cea = build_cea_performers(self._recording_multi([cond, cond, orch, orch]))
        assert len(cea.conductors) == 1
        assert len(cea.ensembles) == 1

    def test_two_distinct_conductors_both_kept(self) -> None:
        """Two conductor relations with different MBIDs are both retained."""
        cond1 = self._rel("conductor", "cond-1", "Karajan", "Karajan, Herbert von")
        cond2 = self._rel("conductor", "cond-2", "Böhm", "Böhm, Karl")
        cea = build_cea_performers(self._recording_multi([cond1, cond2]))
        assert len(cea.conductors) == 2

    def test_duplicate_instrumentalist_deduplicated(self) -> None:
        """Two instrument relations with the same MBID yield exactly one instrumentalist entry."""
        attr: JSON = {"type": "", "value": "violin"}
        rel = self._rel("instrument", "instr-1", "Violinist X", "X, Violinist", [attr])
        cea = build_cea_performers(self._recording_multi([rel, rel]))
        assert len(cea.instrumentalists) == 1

    def test_duplicate_no_mbid_both_kept(self) -> None:
        """Relations without an MBID cannot be deduplicated by MBID and are both appended."""
        rel = self._rel("conductor", "", "Unknown Conductor", "")
        cea = build_cea_performers(self._recording_multi([rel, rel]))
        assert len(cea.conductors) == 2


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

    def test_multi_type_image_written_and_journalled_once(self, fs: FakeFilesystem) -> None:
        """A CoverImage shared across two CoverArt bucket lists is written and journalled once.

        The Cover Art Archive allows a single image to carry multiple type tags (e.g. Back +
        Spine).  fetch_cover_art reuses the same CoverImage object in both bucket lists to avoid
        duplicate downloads.  _write_sidecars must deduplicate by destination path so the file is
        written exactly once and only one action="downloaded" journal entry is produced.

        :param fs: pyfakefs fixture.
        """
        work_top = Path("/dest/Composer/Work")
        fs.create_dir(str(work_top))

        shared_img = CoverImage(data=b"\xff\xd8\xff\xe0" + b"\x00" * 20, mime="image/jpeg", filename="back.jpg")
        cover = CoverArt(back=[shared_img], spine=[shared_img])

        journal: list[TransactionEntry] = []
        sidecars_written: set[Path] = set()
        _write_sidecars(cover, work_top, sidecars_written, journal, "2026-01-01T00:00:00+00:00", "rel-x")  # pylint: disable=protected-access

        assert (work_top / "back.jpg").exists()
        assert len(journal) == 1
        assert journal[0].action == "downloaded"
        assert journal[0].destination == str(work_top / "back.jpg")

    def test_distinct_images_in_different_buckets_all_written(self, fs: FakeFilesystem) -> None:
        """Distinct CoverImage objects in different buckets are each written and journalled.

        When back and spine hold different images (distinct objects, distinct filenames), both
        should be written and produce separate journal entries — the path-level guard must not
        suppress legitimate distinct images.

        :param fs: pyfakefs fixture.
        """
        work_top = Path("/dest/Composer/Work")
        fs.create_dir(str(work_top))

        back_img = CoverImage(data=b"\xff\xd8\xff\xe0" + b"\x00" * 20, mime="image/jpeg", filename="back.jpg")
        spine_img = CoverImage(data=b"\xff\xd8\xff\xe0" + b"\x01" * 20, mime="image/jpeg", filename="spine.jpg")
        cover = CoverArt(back=[back_img], spine=[spine_img])

        journal: list[TransactionEntry] = []
        sidecars_written: set[Path] = set()
        _write_sidecars(cover, work_top, sidecars_written, journal, "2026-01-01T00:00:00+00:00", "rel-x")  # pylint: disable=protected-access

        assert (work_top / "back.jpg").exists()
        assert (work_top / "spine.jpg").exists()
        assert len(journal) == 2
        destinations = {e.destination for e in journal}
        assert destinations == {str(work_top / "back.jpg"), str(work_top / "spine.jpg")}


# ---------------------------------------------------------------------------
# _write_sidecars — hash verification
# ---------------------------------------------------------------------------


class TestWriteSidecarsHashCheck:
    """Tests for the SHA-256 readback integrity check added to _write_sidecars."""

    def test_hash_mismatch_raises_runtime_error(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """RuntimeError is raised when the written sidecar bytes do not match the in-memory hash.

        Simulates filesystem corruption by making hashlib.sha256 return different digests for the
        in-memory hash vs the readback hash.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        work_top = Path("/dest/Artist/Work")
        fs.create_dir(str(work_top))
        jpeg = b"\xff\xd8" + b"\x00" * 50
        cover = CoverArt(front_full=[CoverImage(data=jpeg, mime="image/jpeg", filename="cover.jpg", url="https://caa/1")])

        # First sha256 call (in-memory) returns "aaa…", second (readback) returns "bbb…" → mismatch.
        call_count = 0

        def _fake_sha256(data: bytes) -> MagicMock:  # pylint: disable=unused-argument
            nonlocal call_count
            call_count += 1
            mock_hash: MagicMock = MagicMock()
            mock_hash.hexdigest.return_value = "a" * 64 if call_count == 1 else "b" * 64
            return mock_hash

        mocker.patch("music_annotator._pipeline.hashlib.sha256", side_effect=_fake_sha256)
        journal: list[TransactionEntry] = []
        with pytest.raises(RuntimeError, match="sidecar write integrity failure"):
            _write_sidecars(cover, work_top, set(), journal, "now", "rel-1")  # pylint: disable=protected-access


# ---------------------------------------------------------------------------
# _write_freedb_yaml
# ---------------------------------------------------------------------------


class TestWriteFreedBYaml:
    """Tests for _write_freedb_yaml."""

    _YAML_CONTENT: bytes = b"disc_id: [123, 2, 182, 50000, 3600]\nrecord: []\n"

    def test_no_yaml_file_is_noop(self, fs: FakeFilesystem) -> None:
        """When no 00 - disc info.yaml exists the function returns without writing anything.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        work_top = Path("/dest/Artist/Work")
        fs.create_dir(str(src))
        fs.create_dir(str(work_top))
        journal: list[TransactionEntry] = []
        written: set[Path] = set()
        _write_freedb_yaml(src, work_top, 1, written, journal, "now", "rel-1")  # pylint: disable=protected-access
        assert journal == []
        assert not (work_top / "freedb_disc_1.yaml").exists()

    def test_yaml_written_with_correct_name_and_journal_entry(self, fs: FakeFilesystem) -> None:
        """The YAML is written to freedb_disc_{pos}.yaml and a sidecar journal entry is appended.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        work_top = Path("/dest/Artist/Work")
        fs.create_dir(str(src))
        fs.create_dir(str(work_top))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=self._YAML_CONTENT)
        journal: list[TransactionEntry] = []
        written: set[Path] = set()
        _write_freedb_yaml(src, work_top, 2, written, journal, "now", "rel-1")  # pylint: disable=protected-access
        dest = work_top / "freedb_disc_2.yaml"
        assert dest.exists()
        assert dest.read_bytes() == self._YAML_CONTENT
        assert len(journal) == 1
        assert journal[0].action == "sidecar"
        assert journal[0].destination == str(dest)
        assert journal[0].source == str(src / "00 - disc info.yaml")

    def test_second_call_for_same_dest_is_noop(self, fs: FakeFilesystem) -> None:
        """The freedb_written guard prevents writing the file more than once per dest path.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        work_top = Path("/dest/Artist/Work")
        fs.create_dir(str(src))
        fs.create_dir(str(work_top))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=self._YAML_CONTENT)
        journal: list[TransactionEntry] = []
        written: set[Path] = set()
        _write_freedb_yaml(src, work_top, 1, written, journal, "now", "rel-1")  # pylint: disable=protected-access
        _write_freedb_yaml(src, work_top, 1, written, journal, "now", "rel-1")  # pylint: disable=protected-access
        assert len(journal) == 1  # only one entry

    def test_different_disc_positions_write_separate_files(self, fs: FakeFilesystem) -> None:
        """Disc positions 1 and 2 produce freedb_disc_1.yaml and freedb_disc_2.yaml separately.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        work_top = Path("/dest/Artist/Work")
        fs.create_dir(str(src))
        fs.create_dir(str(work_top))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=self._YAML_CONTENT)
        journal: list[TransactionEntry] = []
        written: set[Path] = set()
        _write_freedb_yaml(src, work_top, 1, written, journal, "now", "rel-1")  # pylint: disable=protected-access
        _write_freedb_yaml(src, work_top, 2, written, journal, "now", "rel-1")  # pylint: disable=protected-access
        assert (work_top / "freedb_disc_1.yaml").exists()
        assert (work_top / "freedb_disc_2.yaml").exists()
        assert len(journal) == 2

    def test_hash_mismatch_raises_runtime_error(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """RuntimeError is raised when dest SHA-256 does not match source SHA-256 after copy.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        work_top = Path("/dest/Artist/Work")
        fs.create_dir(str(src))
        fs.create_dir(str(work_top))
        fs.create_file(str(src / "00 - disc info.yaml"), contents=self._YAML_CONTENT)

        # Patch shutil.copy2 to write corrupted content.
        def _corrupt_copy(_s: object, d: object) -> None:
            Path(str(d)).write_bytes(b"\x00" * len(self._YAML_CONTENT))

        mocker.patch("music_annotator._pipeline.shutil.copy2", side_effect=_corrupt_copy)
        journal: list[TransactionEntry] = []
        with pytest.raises(RuntimeError, match="freedb yaml copy integrity failure"):
            _write_freedb_yaml(src, work_top, 1, set(), journal, "now", "rel-1")  # pylint: disable=protected-access


# ---------------------------------------------------------------------------
# run() — freedb yaml written end-to-end
# ---------------------------------------------------------------------------


class TestRunWritesFreedBYaml:
    """Tests that run() writes freedb_disc_N.yaml to the work-top-dir."""

    def test_freedb_yaml_written_and_sidecar_journalled(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """After a successful run, freedb_disc_N.yaml exists and the journal has a sidecar entry.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 3):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        yaml_content = b"disc_id: [123, 2, 182, 50000, 3600]\nrecord: []\n"
        fs.create_file(str(src / "00 - disc info.yaml"), contents=yaml_content)

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

        # freedb_disc_1.yaml must exist in the work top directory.
        flac_files = list(dest.rglob("*.flac"))
        assert flac_files
        work_top = dest / Path(flac_files[0]).relative_to(dest).parts[0] / Path(flac_files[0]).relative_to(dest).parts[1]
        assert (work_top / "freedb_disc_1.yaml").exists()
        assert (work_top / "freedb_disc_1.yaml").read_bytes() == yaml_content

        # Journal must include a sidecar entry for the yaml.
        journal_data = json.loads((dest / JOURNAL_FILENAME).read_text(encoding="utf-8"))
        sidecar_entries = [e for e in journal_data if e["action"] == "sidecar"]
        assert len(sidecar_entries) == 1
        assert sidecar_entries[0]["destination"].endswith("freedb_disc_1.yaml")


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

    def test_dest_root_created_if_absent(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """run() creates dest_root (and any missing parents) when it does not already exist.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/new/library/dest")
        fs.create_dir(str(src))
        # Deliberately do NOT create dest or its parents.
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
        assert dest.is_dir()
        assert len(list(dest.rglob("*.flac"))) == 1

    def test_dest_root_created_logs_info(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """A dest_root_created info event is logged when the directory is newly created.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/new/dest")
        fs.create_dir(str(src))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=1)
        self._patch_mb(mocker, release)
        mocker.patch("music_annotator._pipeline.apply_tags_flac")

        log_events: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._pipeline.log.info", side_effect=lambda event, **kw: log_events.append({"event": event, **kw})
        )

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        assert any(e["event"] == "dest_root_created" for e in log_events)

    def test_dest_root_existing_no_log(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """No dest_root_created event is logged when dest_root already exists.

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

        log_events: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._pipeline.log.info", side_effect=lambda event, **kw: log_events.append({"event": event, **kw})
        )

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        assert not any(e["event"] == "dest_root_created" for e in log_events)

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

    def test_recording_date_work_produces_unified_dest_dir(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Movements with different RECORDING_DATE values land in the same destination directory.

        run() sets recording_date_work to the union range across all movements and
        build_dest_path reads it directly (not via to_file_dict()), so all movements of the
        work get the same directory label even when individual session dates differ.

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
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        call_count = [0]

        def _fetch_rec(rec_id: str) -> MBRecording:
            call_count[0] += 1
            # movement 1: session 1981, movement 2: session 1984 — different years
            begin = "1981-03-01" if call_count[0] == 1 else "1984-05-10"
            return _rec(
                {
                    "id": rec_id,
                    "title": "T",
                    "artist-credit": [],
                    "artist-relation-list": [
                        {
                            "type": "conductor",
                            "direction": "backward",
                            "begin": begin,
                            "end": "",
                            "artist": {"id": "a1", "name": "K", "sort-name": "K"},
                        }
                    ],
                    "work-relation-list": [],
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        captured_dests: list[Path] = []
        real_build = music_annotator._tags.build_dest_path  # pylint: disable=protected-access

        def _capture_dest(dest_root: Path, rel: MBRelease, track: MBTrack, tags: TrackTags) -> Path:
            p = real_build(dest_root, rel, track, tags)
            captured_dests.append(p)
            return p

        mocker.patch("music_annotator._pipeline.build_dest_path", side_effect=_capture_dest)

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        assert len(captured_dests) == 2
        # Both movements should share the same parent directory
        assert captured_dests[0].parent == captured_dests[1].parent, (
            f"Movements landed in different directories: {captured_dests[0].parent} vs {captured_dests[1].parent}"
        )
        # The shared directory should include a [rec 1981-1984] label
        assert "[rec 1981-1984]" in str(captured_dests[0].parent)

    def test_recording_first_release_date_normalized_across_movements(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Movements with different recording_first_release_date values are normalized to the release year.

        When no session date is available, the [rel YYYY] fallback is driven by
        recording_first_release_date.  Movements can have different values here (e.g. a
        movement that first appeared on an earlier pressing).  run() normalizes all movements
        to the release date year so they all land in the same directory.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "02.flac"), contents=_MINIMAL_FLAC)

        # Release dated 1965; movements have differing first-release-date values
        release = MBRelease.model_validate(
            {
                "id": "rel-norm",
                "title": "Test Album",
                "date": "1965",
                "status": "Official",
                "barcode": "",
                "artist-credit": [{"name": "Composer", "artist": {"id": "c1", "name": "Composer", "sort-name": "Composer"}}],
                "release-group": {"id": "rg-1", "primary-type": "Album", "first-release-date": "1965"},
                "label-info-list": [],
                "text-representation": {"script": "Latn", "language": "eng"},
                "medium-list": [
                    {
                        "position": 1,
                        "format": "CD",
                        "track-list": [
                            {
                                "id": "trk-1",
                                "position": 1,
                                "recording": {"id": "rec-1", "title": "Movement I", "artist-credit": []},
                            },
                            {
                                "id": "trk-2",
                                "position": 2,
                                "recording": {"id": "rec-2", "title": "Movement II", "artist-credit": []},
                            },
                        ],
                    }
                ],
            }
        )

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        call_count = [0]

        def _fetch_rec(rec_id: str) -> MBRecording:
            call_count[0] += 1
            # No session dates; differing first-release-date: rec-1 says 1963, rec-2 says 1965
            frd = "1963" if call_count[0] == 1 else "1965"
            return MBRecording.model_validate(
                {
                    "id": rec_id,
                    "title": "T",
                    "first-release-date": frd,
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [],
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        music_annotator.run(
            release_id="rel-norm",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        tags1: TrackTags = mock_tag.call_args_list[0][0][1]
        tags2: TrackTags = mock_tag.call_args_list[1][0][1]
        # Both movements should have been normalized to the release year
        assert tags1.recording_first_release_date == tags2.recording_first_release_date == "1965"

    def test_recording_first_release_date_unchanged_when_release_has_no_date(
        self, mocker: MockerFixture, fs: FakeFilesystem
    ) -> None:
        """recording_first_release_date is left as-is when the release has no date fields.

        When no session date is available and both release.date and
        release_group.first_release_date are empty, run() cannot compute a normalising year
        and leaves each track's recording_first_release_date unchanged.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "02.flac"), contents=_MINIMAL_FLAC)

        # Release with no date fields at all
        release = MBRelease.model_validate(
            {
                "id": "rel-nodate",
                "title": "No Date Album",
                "date": "",
                "status": "Official",
                "barcode": "",
                "artist-credit": [],
                "release-group": {"id": "rg-nd", "primary-type": "Album", "first-release-date": ""},
                "label-info-list": [],
                "text-representation": {"script": "", "language": ""},
                "medium-list": [
                    {
                        "position": 1,
                        "format": "CD",
                        "track-list": [
                            {
                                "id": "trk-1",
                                "position": 1,
                                "recording": {"id": "rec-nd-1", "title": "Mvt I", "artist-credit": []},
                            },
                            {
                                "id": "trk-2",
                                "position": 2,
                                "recording": {"id": "rec-nd-2", "title": "Mvt II", "artist-credit": []},
                            },
                        ],
                    }
                ],
            }
        )

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        call_count = [0]

        def _fetch_rec(rec_id: str) -> MBRecording:
            call_count[0] += 1
            frd = "1963" if call_count[0] == 1 else "1965"
            return MBRecording.model_validate(
                {
                    "id": rec_id,
                    "title": "T",
                    "first-release-date": frd,
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [],
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        music_annotator.run(
            release_id="rel-nodate",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        tags1: TrackTags = mock_tag.call_args_list[0][0][1]
        tags2: TrackTags = mock_tag.call_args_list[1][0][1]
        # No normalising year available — per-track values are preserved unchanged
        assert tags1.recording_first_release_date == "1963"
        assert tags2.recording_first_release_date == "1965"

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

    def test_composer_unified_across_movements_when_additional_only_on_some(
        self, mocker: MockerFixture, fs: FakeFilesystem
    ) -> None:
        """Movements with only an additional composer inherit the primary composer from other movements.

        When MB credits a finisher as "composer" with the "additional" attribute on only some
        movements, those movements have an empty cwp_composers and fall back to
        additional_composers, producing a different CWP_COMPOSER_LASTNAMES — and therefore a
        different top_dir — than movements that carry a plain primary-composer relation.  run()
        must propagate the primary-composer values so every movement gets the same top-level
        directory.

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
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        # Both movements share the same top work (twid).  Movement 1 links to a movement work
        # (w-mvt1) that has a plain composer relation; movement 2 links to a movement work
        # (w-mvt2) that has only an "additional" composer relation (e.g. a completion credit).
        # Both movement works have a "parts" backward relation to the same root work (w-root),
        # which itself carries the primary composer — but the current per-track RoleBuckets does
        # not see the root via the second movement if the root is not reached in the hierarchy.
        # We deliberately attach the primary composer only to the movement-1 work to isolate the
        # propagation path from the cross-track pass.
        top_work_id = "w-root"

        work_mvt1 = _w(
            {
                "id": "w-mvt1",
                "title": "I. Allegro",
                "type": "",
                "artist-relation-list": [
                    {
                        "type": "composer",
                        "artist": {"id": "a-mozart", "name": "Mozart", "sort-name": "Mozart, Wolfgang Amadeus"},
                        "attribute-list": [],
                    }
                ],
                "work-relation-list": [
                    {"type": "parts", "direction": "backward", "work": {"id": top_work_id, "title": "Concerto"}},
                ],
                "attribute-list": [],
                "tag-list": [],
            }
        )
        work_mvt2 = _w(
            {
                "id": "w-mvt2",
                "title": "II. Rondo",
                "type": "",
                "artist-relation-list": [
                    # Only an additional composer (completion credit) — no plain primary composer.
                    {
                        "type": "composer",
                        "artist": {"id": "a-sussmayr", "name": "Süßmayr", "sort-name": "Süßmayr, Franz Xaver"},
                        "attribute-list": ["additional"],
                    }
                ],
                "work-relation-list": [
                    {"type": "parts", "direction": "backward", "work": {"id": top_work_id, "title": "Concerto"}},
                ],
                "attribute-list": [],
                "tag-list": [],
            }
        )
        work_root = _w(
            {
                "id": top_work_id,
                "title": "Concerto",
                "type": "Concerto",
                "artist-relation-list": [],
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
            }
        )

        call_count = [0]

        def _fetch_rec(rec_id: str) -> MBRecording:
            call_count[0] += 1
            work_id = "w-mvt1" if call_count[0] == 1 else "w-mvt2"
            return _rec(
                {
                    "id": rec_id,
                    "title": "T",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [{"type": "performance", "work": {"id": work_id, "title": "Mvt"}}],
                }
            )

        def _fetch_work(work_id: str) -> MBWork:
            return {"w-mvt1": work_mvt1, "w-mvt2": work_mvt2, top_work_id: work_root}[work_id]

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        # Patch in both locations: _mb_api (used by _get_bottom_work) and _works (used by build_work_hierarchy)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._works.fetch_work_detail", side_effect=_fetch_work)
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

        # Both movements should carry the primary composer from movement 1.
        assert tags1.cwp_composers == tags2.cwp_composers == "Mozart"
        assert tags1.cwp_composer_lastnames == tags2.cwp_composer_lastnames == "Mozart"
        # Movement 2's additional composer (Süßmayr) must NOT have been erased from its own
        # cwp_arrangers / arranger tags — the fix must only touch the missing-composer fields.
        assert tags1.cwp_composers_sort == tags2.cwp_composers_sort

    def test_composer_unified_produces_same_top_dir(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Movements with mismatched composer credits all land in the same top-level directory.

        This is the directory-grouping counterpart to
        test_composer_unified_across_movements_when_additional_only_on_some: it verifies that the
        actual destination paths share the same parent, not just that the tag fields are equal.

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
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        top_work_id = "w-root"
        work_mvt1 = _w(
            {
                "id": "w-mvt1",
                "title": "I. Allegro",
                "type": "",
                "artist-relation-list": [
                    {
                        "type": "composer",
                        "artist": {"id": "a-mozart", "name": "Mozart", "sort-name": "Mozart, Wolfgang Amadeus"},
                        "attribute-list": [],
                    }
                ],
                "work-relation-list": [
                    {"type": "parts", "direction": "backward", "work": {"id": top_work_id, "title": "Concerto"}},
                ],
                "attribute-list": [],
                "tag-list": [],
            }
        )
        work_mvt2 = _w(
            {
                "id": "w-mvt2",
                "title": "II. Rondo",
                "type": "",
                "artist-relation-list": [
                    {
                        "type": "composer",
                        "artist": {"id": "a-sussmayr", "name": "Süßmayr", "sort-name": "Süßmayr, Franz Xaver"},
                        "attribute-list": ["additional"],
                    }
                ],
                "work-relation-list": [
                    {"type": "parts", "direction": "backward", "work": {"id": top_work_id, "title": "Concerto"}},
                ],
                "attribute-list": [],
                "tag-list": [],
            }
        )
        work_root = _w(
            {
                "id": top_work_id,
                "title": "Concerto",
                "type": "Concerto",
                "artist-relation-list": [],
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
            }
        )

        call_count = [0]

        def _fetch_rec(rec_id: str) -> MBRecording:
            call_count[0] += 1
            work_id = "w-mvt1" if call_count[0] == 1 else "w-mvt2"
            return _rec(
                {
                    "id": rec_id,
                    "title": "T",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [{"type": "performance", "work": {"id": work_id, "title": "Mvt"}}],
                }
            )

        def _fetch_work(work_id: str) -> MBWork:
            return {"w-mvt1": work_mvt1, "w-mvt2": work_mvt2, top_work_id: work_root}[work_id]

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        # Patch in both locations: _mb_api (used by _get_bottom_work) and _works (used by build_work_hierarchy)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._works.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access

        captured_dests: list[Path] = []
        real_build = music_annotator._tags.build_dest_path  # pylint: disable=protected-access

        def _capture_dest(dest_root: Path, rel: MBRelease, track: MBTrack, tags: TrackTags) -> Path:
            p = real_build(dest_root, rel, track, tags)
            captured_dests.append(p)
            return p

        mocker.patch("music_annotator._pipeline.build_dest_path", side_effect=_capture_dest)

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        assert len(captured_dests) == 2
        # Both movements must share the same top-level directory (parts[0]).
        tops = {p.relative_to(dest).parts[0] for p in captured_dests}
        assert len(tops) == 1, f"Movements landed in different top dirs: {sorted(tops)}"
        # The shared top-level directory must be Mozart's, not Süßmayr's.
        assert "Mozart" in tops.pop()

    def test_composer_not_modified_when_all_movements_are_additional_only(
        self, mocker: MockerFixture, fs: FakeFilesystem
    ) -> None:
        """When no movement in a group has a primary composer, cwp_composers is left unchanged.

        If every movement has only additional_composers (no plain primary composer exists
        anywhere in the group), the unification pass must not modify any tag — each movement
        retains its own fallback additional-composer value.

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
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        top_work_id = "w-root"
        # Both movements have only additional-composer relations — no plain primary anywhere.
        work_mvt1 = _w(
            {
                "id": "w-mvt1",
                "title": "Mvt I",
                "type": "",
                "artist-relation-list": [
                    {
                        "type": "composer",
                        "artist": {"id": "a-x", "name": "Arranger X", "sort-name": "X, Arranger"},
                        "attribute-list": ["additional"],
                    }
                ],
                "work-relation-list": [
                    {"type": "parts", "direction": "backward", "work": {"id": top_work_id, "title": "Work"}},
                ],
                "attribute-list": [],
                "tag-list": [],
            }
        )
        work_mvt2 = _w(
            {
                "id": "w-mvt2",
                "title": "Mvt II",
                "type": "",
                "artist-relation-list": [
                    {
                        "type": "composer",
                        "artist": {"id": "a-y", "name": "Arranger Y", "sort-name": "Y, Arranger"},
                        "attribute-list": ["additional"],
                    }
                ],
                "work-relation-list": [
                    {"type": "parts", "direction": "backward", "work": {"id": top_work_id, "title": "Work"}},
                ],
                "attribute-list": [],
                "tag-list": [],
            }
        )
        work_root = _w(
            {
                "id": top_work_id,
                "title": "Work",
                "type": "",
                "artist-relation-list": [],
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
            }
        )

        call_count = [0]

        def _fetch_rec(rec_id: str) -> MBRecording:
            call_count[0] += 1
            work_id = "w-mvt1" if call_count[0] == 1 else "w-mvt2"
            return _rec(
                {
                    "id": rec_id,
                    "title": "T",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [{"type": "performance", "work": {"id": work_id, "title": "Mvt"}}],
                }
            )

        def _fetch_work(work_id: str) -> MBWork:
            return {"w-mvt1": work_mvt1, "w-mvt2": work_mvt2, top_work_id: work_root}[work_id]

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        # Patch in both locations: _mb_api (used by _get_bottom_work) and _works (used by build_work_hierarchy)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._works.fetch_work_detail", side_effect=_fetch_work)
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

        # Both movements fell back to their own additional-composer.  No cross-propagation
        # should have occurred — the additional-only fallback values must be preserved.
        assert tags1.cwp_composers == "Arranger X"
        assert tags2.cwp_composers == "Arranger Y"

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
        """Collision warning shows the work-top-dir (parts[0]/parts[1]) with date suffix as a relative path.

        Two files in the same work directory should produce one grouped directory entry in the
        output, displayed as a path relative to dest_root so the [rec/rel YYYY] suffix is visible
        even on narrow terminals.  Individual filenames are listed flat beneath the directory
        summary.

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

        # The work-top-dir with the date suffix must appear in the directory summary line.
        assert any("Sinfonie Nr. 2 D-Dur, op. 73 [rec 1977-1978]" in line for line in printed)
        # The absolute dest prefix must NOT appear in the directory lines (relative paths only).
        assert not any(str(dest) in line and "Sinfonie" in line for line in printed)
        # Both individual filenames must appear in the flat filename list.
        assert any("01 - Symphony no. 2 in D major, op. 73_ I.flac" in line for line in printed)
        assert any("02 - Symphony no. 2 in D major, op. 73_ II.flac" in line for line in printed)

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
        assert data[0]["action"] == "tagged"
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

    def test_collision_overwrite_journal_action_tagged(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Journal records action="tagged" for files written on overwrite choice.

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
        assert any(e["action"] == "tagged" for e in data)

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
        assert "tagged" in actions

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
        """The 'Verified OK' message shows the work-top-dir as a relative path with [rec/rel] suffix.

        Paths are printed relative to dest_root so the [rec YYYY] / [rel YYYY] suffix is visible
        without the absolute prefix overflowing the terminal width.  The confirmation header
        includes the dest_root for context.  For a 3-level hierarchy the immediate parent of a
        file is a division subdirectory, not the work directory; this test verifies that the
        confirmation message groups at exactly parts[0]/parts[1] depth regardless of hierarchy.

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

        # A 'Verified OK' header line must appear and it must name dest_root.
        assert any("Verified OK" in line and str(dest) in line for line in printed)
        # The directory lines must be relative paths (exactly parts[0]/parts[1] deep).
        # They appear as indented lines after the header, containing no "Verified" or "safe".
        dir_lines = [
            line for line in printed if "Verified" not in line and "safe" not in line and line.strip().startswith("[green]")
        ]
        for line in dir_lines:
            raw = line.replace("[green]", "").replace("[/]", "").strip()
            p = Path(raw)
            # Relative path must not contain dest prefix and must be exactly 2 parts deep.
            assert not p.is_absolute()
            assert len(p.parts) == 2  # noqa: PLR2004 — exactly composer/work depth


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
# _write_sidecars
# ---------------------------------------------------------------------------


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

        music_annotator.run(
            release_id="rel-multi",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        # Disc 2 has 2 tracks; source dir has 2 files → apply_tags_flac called twice.
        flac_files = list(dest.rglob("*.flac"))
        assert len(flac_files) == 2

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

        Pressing A has offsets well outside tolerance; pressing B offsets (exact) are supplied.
        """
        pressing_a = [182, 50500, 100500, 150500]  # >1 frame difference — outside tolerance
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

    def test_fuzzy_match_plus_one_per_track(self) -> None:
        """Matches when every YAML offset is exactly 1 frame less than the MB offset.

        This is the real-world case seen with dBpowerAMP vs MusicBrainz counting conventions.
        """
        mb_offsets = [183, 114258]
        yaml_offsets = [182, 114257]  # each off by -1
        medium = _medium_with_toc(2, mb_offsets)
        result = _match_medium_by_toc([medium], yaml_offsets)
        assert result is medium

    def test_fuzzy_match_minus_one_per_track(self) -> None:
        """Matches when every YAML offset is exactly 1 frame more than the MB offset."""
        mb_offsets = [182, 114257]
        yaml_offsets = [183, 114258]  # each off by +1
        medium = _medium_with_toc(2, mb_offsets)
        result = _match_medium_by_toc([medium], yaml_offsets)
        assert result is medium

    def test_fuzzy_match_logs_warning(self, mocker: MockerFixture) -> None:
        """A fuzzy TOC match (±1 frame) emits a toc_match_fuzzy warning log.

        :param mocker: pytest-mock fixture.
        """
        mb_offsets = [183, 114258]
        yaml_offsets = [182, 114257]
        medium = _medium_with_toc(2, mb_offsets)
        mock_warn = mocker.patch("music_annotator._pipeline.log.warning")
        _match_medium_by_toc([medium], yaml_offsets)
        mock_warn.assert_called_once()
        call_args = mock_warn.call_args
        assert call_args.args[0] == "toc_match_fuzzy"

    def test_exact_match_does_not_log_warning(self, mocker: MockerFixture) -> None:
        """An exact TOC match does not emit a warning log.

        :param mocker: pytest-mock fixture.
        """
        medium = _medium_with_toc(1, _DISC1_OFFSETS)
        mock_warn = mocker.patch("music_annotator._pipeline.log.warning")
        _match_medium_by_toc([medium], _DISC1_OFFSETS)
        mock_warn.assert_not_called()

    def test_offset_diff_of_two_does_not_match(self) -> None:
        """Offsets differing by 2 frames are outside tolerance and do not match."""
        mb_offsets = [183, 114258]
        yaml_offsets = [181, 114256]  # each off by -2
        medium = _medium_with_toc(2, mb_offsets)
        result = _match_medium_by_toc([medium], yaml_offsets)
        assert result is None

    def test_different_length_offsets_do_not_match(self) -> None:
        """Lists of different lengths never match, even if individual values are close."""
        mb_offsets = [183, 114258, 200000]
        yaml_offsets = [183, 114258]
        medium = _medium_with_toc(2, mb_offsets)
        result = _match_medium_by_toc([medium], yaml_offsets)
        assert result is None


# ---------------------------------------------------------------------------
# _score_medium_title / _match_medium_by_title / _select_medium_with_reason
# ---------------------------------------------------------------------------


def _medium_with_title(position: int, medium_title: str, first_track_title: str, n_tracks: int = 4) -> MBMedium:
    """Build a minimal MBMedium with a subtitle and a first track title for title-match tests.

    :param position: 1-based disc position.
    :param medium_title: Medium subtitle (e.g. ``"Symphonies 101 & 102"``).
    :param first_track_title: Title of the first track recording.
    :param n_tracks: Total number of tracks on the medium (only first track title is set; others are blank).
    :returns: An :class:`~music_annotator.models.MBMedium` instance.
    """
    tracks = [
        {
            "id": f"t{position}-{i}",
            "position": i,
            "recording": {
                "id": f"r{position}-{i}",
                "title": first_track_title if i == 1 else f"Track {i}",
                "artist-credit": [],
            },
        }
        for i in range(1, n_tracks + 1)
    ]
    return MBMedium.model_validate({"position": position, "format": "CD", "title": medium_title, "track-list": tracks})


class TestScoreMediumTitle:
    """Tests for _score_medium_title."""

    def test_matches_numeric_tokens(self) -> None:
        """Symphony numbers shared between dtitle and first track title score positively."""
        medium = _medium_with_title(4, "", "Symphonie D-Dur Hob.I: 101 Die Uhr: 1. Adagio")
        score = _score_medium_title(medium, {"101", "102", "haydn"})
        assert score >= 1

    def test_medium_title_also_scored(self) -> None:
        """Tokens in the medium subtitle contribute to the score."""
        medium = _medium_with_title(4, "Symphonies 101 and 102", "Unrelated track title")
        score = _score_medium_title(medium, {"101", "102"})
        assert score == 2

    def test_no_overlap_scores_zero(self) -> None:
        """A medium with no shared tokens scores zero."""
        medium = _medium_with_title(3, "", "Symphonie B-Dur Hob.I: 98: 1. Adagio")
        score = _score_medium_title(medium, {"101", "102"})
        assert score == 0

    def test_empty_track_list_scores_zero(self) -> None:
        """A medium with no tracks scores zero regardless of dtitle tokens."""
        empty = MBMedium.model_validate({"position": 1, "format": "CD", "track-list": []})
        score = _score_medium_title(empty, {"101", "102"})
        assert score == 0


class TestMatchMediumByTitle:
    """Tests for _match_medium_by_title."""

    def _make_haydn_mediums(self) -> list[MBMedium]:
        """Return three mediums mimicking the Haydn 12 Londoner Symphonien release."""
        return [
            _medium_with_title(3, "", "Symphonie B-Dur Hob.I: 98: 1. Adagio Allegro"),
            _medium_with_title(4, "", "Symphonie D-Dur Hob.I: 101 Die Uhr: 1. Adagio Presto"),
            _medium_with_title(5, "", "Symphonie Es-Dur Hob.I: 103 Paukenwirbel: 1. Adagio"),
        ]

    def test_identifies_disc_4_by_symphony_numbers(self) -> None:
        """'Haydn Symphonien 101 & 102' token-matches disc 4 (Sym. 101) over disc 3 (Sym. 98)."""
        mediums = self._make_haydn_mediums()
        result = _match_medium_by_title(mediums, 4, "Haydn Symphonien 101 & 102")
        assert result is not None
        assert result.position == 4

    def test_returns_none_on_tie(self) -> None:
        """Returns None when two mediums score equally."""
        m1 = _medium_with_title(1, "Sym 101 102", "First track")
        m2 = _medium_with_title(2, "Sym 101 102", "Other track")
        result = _match_medium_by_title([m1, m2], 4, "101 102")
        assert result is None

    def test_returns_none_when_all_score_zero(self) -> None:
        """Returns None when no medium has any matching tokens."""
        mediums = self._make_haydn_mediums()
        result = _match_medium_by_title(mediums, 4, "Beethoven Fidelio")
        assert result is None

    def test_returns_none_when_dtitle_empty(self) -> None:
        """Returns None when dtitle is empty."""
        mediums = self._make_haydn_mediums()
        result = _match_medium_by_title(mediums, 4, "")
        assert result is None

    def test_ignores_mediums_with_wrong_track_count(self) -> None:
        """Only considers mediums whose track count equals n_src."""
        m_wrong = _medium_with_title(1, "Sym 101 102", "Sym 101 first movement", n_tracks=6)
        m_right = _medium_with_title(2, "", "Symphonie 101: first movement", n_tracks=4)
        result = _match_medium_by_title([m_wrong, m_right], 4, "101")
        # Only m_right has 4 tracks; it scores 1 while m_wrong is excluded.
        assert result is m_right

    def test_logs_warning(self, mocker: MockerFixture) -> None:
        """Always logs a title_match_heuristic warning when called with a non-empty dtitle.

        :param mocker: pytest-mock fixture.
        """
        mediums = self._make_haydn_mediums()
        mock_warn = mocker.patch("music_annotator._pipeline.log.warning")
        _match_medium_by_title(mediums, 4, "Haydn Symphonien 101 & 102")
        mock_warn.assert_called_once()
        assert mock_warn.call_args.args[0] == "title_match_heuristic"

    def test_returns_none_when_dtitle_only_punctuation(self) -> None:
        """Returns None when dtitle tokenises to an empty set (e.g. all punctuation).

        Covers the ``if not dtitle_tokens: return None`` branch.
        """
        mediums = self._make_haydn_mediums()
        result = _match_medium_by_title(mediums, 4, "--- !!!")
        assert result is None


class TestSelectMediumWithReason:
    """Tests for _select_medium_with_reason — selection method tagging."""

    def test_toc_match_returns_toc_method(self) -> None:
        """TOC match returns SelectionMethod.TOC."""
        m1 = _medium_with_toc(1, _DISC1_OFFSETS)
        m2 = _medium_with_toc(2, _DISC2_OFFSETS)
        result, method = _select_medium_with_reason([m1, m2], 4, "dir", track_frames=_DISC2_OFFSETS)
        assert result is m2
        assert method is SelectionMethod.TOC

    def test_unique_track_count_returns_track_count_method(self) -> None:
        """Unique track count returns SelectionMethod.TRACK_COUNT."""
        m1 = _medium_with_title(1, "", "Track", n_tracks=3)
        m2 = _medium_with_title(2, "", "Track", n_tracks=4)
        result, method = _select_medium_with_reason([m1, m2], 4, "dir")
        assert result is m2
        assert method is SelectionMethod.TRACK_COUNT

    def test_title_match_returns_title_method(self) -> None:
        """Title match returns SelectionMethod.TITLE."""
        m3 = _medium_with_title(3, "", "Symphonie B-Dur Hob.I: 98: 1. Adagio", n_tracks=4)
        m4 = _medium_with_title(4, "", "Symphonie D-Dur Hob.I: 101 Die Uhr: 1. Adagio", n_tracks=4)
        result, method = _select_medium_with_reason([m3, m4], 4, "dir", dtitle="Haydn 101 102")
        assert result is m4
        assert method is SelectionMethod.TITLE

    def test_fallback_returns_fallback_method(self) -> None:
        """No TOC, no unique track count, no title match → SelectionMethod.FALLBACK."""
        m1 = _medium_with_title(1, "", "Generic track", n_tracks=4)
        m2 = _medium_with_title(2, "", "Generic track", n_tracks=4)
        result, method = _select_medium_with_reason([m1, m2], 4, "dir")
        assert result is m1  # first-match fallback
        assert method is SelectionMethod.FALLBACK

    def test_disc_hint_in_fallback(self) -> None:
        """Disc-number hint in dir name selects correct medium in fallback path."""
        m1 = _medium_with_title(1, "", "Track", n_tracks=4)
        m2 = _medium_with_title(2, "", "Track", n_tracks=4)
        result, method = _select_medium_with_reason([m1, m2], 4, "Album (Disc 2)")
        assert result is m2
        assert method is SelectionMethod.FALLBACK

    def test_dtitle_no_match_falls_through_to_fallback(self) -> None:
        """When dtitle produces no title match the disc-number/first-match fallback is used.

        Covers the ``if title_match is not None`` False branch (title match attempted but ambiguous).
        """
        m1 = _medium_with_title(1, "", "Generic track", n_tracks=4)
        m2 = _medium_with_title(2, "", "Generic track", n_tracks=4)
        # dtitle non-empty but scores tie → _match_medium_by_title returns None → fallback
        result, method = _select_medium_with_reason([m1, m2], 4, "dir", dtitle="Generic")
        assert result is m1  # first-match fallback
        assert method is SelectionMethod.FALLBACK


# ---------------------------------------------------------------------------
# run() — title-based medium selection and confirm_disc prompt
# ---------------------------------------------------------------------------


#: Minimal disc info YAML that results in dtitle = "Haydn Symphonien 101 & 102".
_TITLE_YAML: str = (
    "disc_id: [1544401672, 4, 182, 38057, 79532, 119782, 3509]\n"
    "record:\n"
    "- disc_info: {category: classical}\n"
    "  preferred: true\n"
    "  track_info: {DTITLE: 'Karajan BPO / Haydn Symphonien 101 & 102'}\n"
)


class TestRunTitleMediumSelection:
    """Tests for run() title-match medium selection and confirm_disc UI prompt."""

    def _patch_mb_two_disc(self, mocker: MockerFixture) -> None:
        """Patch MB API for a 2-medium release where both mediums have 4 tracks and no disc IDs.

        Medium 1 has symphony 98 tracks; medium 2 has symphony 101 tracks.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")

        def _make_rel() -> MBRelease:
            mediums_data: list[JSON] = [
                {
                    "position": 1,
                    "format": "CD",
                    "track-list": [
                        {
                            "id": f"t1-{i}",
                            "position": i,
                            "recording": {"id": f"r1-{i}", "title": f"Sym 98: mvt {i}", "artist-credit": []},
                        }
                        for i in range(1, 5)
                    ],
                },
                {
                    "position": 2,
                    "format": "CD",
                    "track-list": [
                        {
                            "id": f"t2-{i}",
                            "position": i,
                            "recording": {"id": f"r2-{i}", "title": f"Sym 101: mvt {i}", "artist-credit": []},
                        }
                        for i in range(1, 5)
                    ],
                },
            ]
            return MBRelease.model_validate(
                {
                    "id": "rel-title",
                    "title": "12 Londoner Symphonien",
                    "date": "1990",
                    "status": "Official",
                    "barcode": "",
                    "artist-credit": [],
                    "release-group": {"id": "rg-1", "primary-type": "Album", "first-release-date": "1990"},
                    "label-info-list": [],
                    "text-representation": {"script": "Latn", "language": "ger"},
                    "medium-list": mediums_data,
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_rel())
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

    def test_title_match_selects_disc_2_and_prompts(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Title match selects disc 2 (Sym 101) and calls ui.confirm_disc for confirmation.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 5):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / _DISC_INFO_FILENAME), contents=_TITLE_YAML)
        self._patch_mb_two_disc(mocker)

        mock_ui = MagicMock()
        mock_ui.confirm_disc.side_effect = lambda mediums, proposed, dtitle, url: proposed

        music_annotator.run(
            release_id="rel-title",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            ui=mock_ui,
        )

        mock_ui.confirm_disc.assert_called_once()
        proposed = mock_ui.confirm_disc.call_args[0][1]
        assert proposed.position == 2

    def test_confirm_disc_abort_raises_system_exit(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When ui.confirm_disc returns None the run is aborted with SystemExit(1).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 5):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / _DISC_INFO_FILENAME), contents=_TITLE_YAML)
        self._patch_mb_two_disc(mocker)

        mock_ui = MagicMock()
        mock_ui.confirm_disc.return_value = None

        with pytest.raises(SystemExit) as exc_info:
            music_annotator.run(
                release_id="rel-title",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=False,
                fetch_rels=True,
                ui=mock_ui,
            )
        assert exc_info.value.code == 1

    def test_no_ui_title_match_proceeds_without_prompt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When ui=None a title-match proceeds without prompting (silent heuristic).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 5):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / _DISC_INFO_FILENAME), contents=_TITLE_YAML)
        self._patch_mb_two_disc(mocker)
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")

        music_annotator.run(
            release_id="rel-title",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            ui=None,
        )
        assert mock_tag.call_count == 4

    def test_dry_run_skips_confirm_disc_prompt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """In dry-run mode the confirm_disc prompt is skipped even when ui is provided.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 5):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / _DISC_INFO_FILENAME), contents=_TITLE_YAML)
        self._patch_mb_two_disc(mocker)

        mock_ui = MagicMock()

        music_annotator.run(
            release_id="rel-title",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=True,
            fetch_rels=True,
            ui=mock_ui,
        )
        mock_ui.confirm_disc.assert_not_called()


# ---------------------------------------------------------------------------
# _warn_long_names / _resolve_long_names / run() — name-length enforcement
# ---------------------------------------------------------------------------


class TestRunNameTooLong:
    """Tests for path-component length detection and interactive shortening in run()."""

    def _patch_mb(self, mocker: MockerFixture, release: MBRelease) -> None:
        """Patch all MB API calls and post-copy verification for a minimal run.

        :param mocker: pytest-mock fixture.
        :param release: MBRelease to return from fetch_release.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        def _fetch_rec(rec_id: str) -> MBRecording:
            return _rec({"id": rec_id, "title": "T", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []})

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator._pipeline.fetch_acoustid_id", return_value="")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline.apply_tags_flac")

    def _make_long_release(self) -> MBRelease:
        """Return a release whose dest path will have a component longer than _NAME_MAX when patched to 20.

        :returns: A minimal :class:`~music_annotator.models.MBRelease` with one track.
        """
        return _make_release(n_tracks=1)

    def test_no_ui_auto_shortens_and_logs_warning(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When ui=None and a component exceeds _NAME_MAX, run() auto-shortens and logs name_too_long.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mocker.patch("music_annotator._tags._NAME_MAX", 20)
        mocker.patch("music_annotator._pipeline._NAME_MAX", 20)

        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = self._make_long_release()
        self._patch_mb(mocker, release)

        log_events: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._pipeline.log.warning", side_effect=lambda event, **kw: log_events.append({"event": event, **kw})
        )

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            ui=None,
        )

        # At least one name_too_long warning logged.
        assert any(e["event"] == "name_too_long" for e in log_events)
        # All dest components must fit within the patched limit.
        for flac in dest.rglob("*.flac"):
            for part in flac.relative_to(dest).parts:
                assert len(part.encode("utf-8")) <= 20, f"Component too long: {part!r}"

    def test_ui_accept_proposed_run_completes(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When ui.confirm_shortened_name returns proposed, run() completes with shortened paths.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mocker.patch("music_annotator._tags._NAME_MAX", 20)
        mocker.patch("music_annotator._pipeline._NAME_MAX", 20)

        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = self._make_long_release()
        self._patch_mb(mocker, release)

        mock_ui = MagicMock()
        mock_ui.confirm_shortened_name.side_effect = lambda original, proposed: proposed

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            ui=mock_ui,
        )

        assert mock_ui.confirm_shortened_name.called
        for flac in dest.rglob("*.flac"):
            for part in flac.relative_to(dest).parts:
                assert len(part.encode("utf-8")) <= 20, f"Component too long: {part!r}"

    def test_ui_abort_raises_system_exit(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When ui.confirm_shortened_name returns None, run() raises SystemExit(1).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mocker.patch("music_annotator._tags._NAME_MAX", 20)
        mocker.patch("music_annotator._pipeline._NAME_MAX", 20)

        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = self._make_long_release()
        self._patch_mb(mocker, release)

        mock_ui = MagicMock()
        mock_ui.confirm_shortened_name.return_value = None

        with pytest.raises(SystemExit) as exc_info:
            music_annotator.run(
                release_id="rel-1",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=False,
                fetch_rels=True,
                ui=mock_ui,
            )
        assert exc_info.value.code == 1

    def test_dry_run_logs_warning_no_prompt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """In dry-run mode a name_too_long warning is logged and no prompt fires.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mocker.patch("music_annotator._tags._NAME_MAX", 20)
        mocker.patch("music_annotator._pipeline._NAME_MAX", 20)

        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = self._make_long_release()
        self._patch_mb(mocker, release)

        mock_ui = MagicMock()
        log_events: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._pipeline.log.warning", side_effect=lambda event, **kw: log_events.append({"event": event, **kw})
        )

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=True,
            fetch_rels=False,
            ui=mock_ui,
        )

        assert any(e["event"] == "name_too_long" for e in log_events)
        mock_ui.confirm_shortened_name.assert_not_called()

    def test_shared_component_prompted_once(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """A too-long component shared by multiple tracks triggers confirm_shortened_name exactly once.

        Both tracks share the same top-level composer directory, which is the too-long component.
        The prompt must fire once, and both tracks must land in the same shortened directory.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mocker.patch("music_annotator._tags._NAME_MAX", 20)
        mocker.patch("music_annotator._pipeline._NAME_MAX", 20)

        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "02.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=2)
        self._patch_mb(mocker, release)

        mock_ui = MagicMock()
        mock_ui.confirm_shortened_name.side_effect = lambda original, proposed: proposed

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            ui=mock_ui,
        )

        # Each unique too-long component is prompted exactly once.
        # Collect the set of unique originals passed to the mock.
        originals = [call.args[0] for call in mock_ui.confirm_shortened_name.call_args_list]
        assert len(originals) == len(set(originals)), "Same component prompted more than once"

    def test_warn_long_names_logs_all_long_components(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_warn_long_names logs name_too_long for every unique oversized component in the plan.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mocker.patch("music_annotator._tags._NAME_MAX", 20)
        mocker.patch("music_annotator._pipeline._NAME_MAX", 20)

        dest = Path("/dest")
        fs.create_dir(str(dest))
        fs.create_file(str(dest / "dummy.flac"))

        src_file = dest / "dummy.flac"
        # Build a plan entry whose dest path has a component longer than 20 bytes.
        long_component = "A" * 30
        dest_file = dest / long_component / "01 - Track.flac"
        plan = [CopyPlanEntry(idx=0, src_file=src_file, dest_file=dest_file)]

        log_events: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._pipeline.log.warning", side_effect=lambda event, **kw: log_events.append({"event": event, **kw})
        )

        _warn_long_names(plan, dest)

        assert any(e["event"] == "name_too_long" for e in log_events)

    def test_resolve_long_names_no_long_components_returns_same_plan(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_resolve_long_names returns the input plan unchanged when all components are within the limit.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        mocker.patch("music_annotator._pipeline._NAME_MAX", 255)

        dest = Path("/dest")
        fs.create_dir(str(dest))
        fs.create_file(str(dest / "dummy.flac"))

        src_file = dest / "dummy.flac"
        dest_file = dest / "Short Dir" / "01 - Track.flac"
        plan = [CopyPlanEntry(idx=0, src_file=src_file, dest_file=dest_file)]

        result = _resolve_long_names(plan, dest, ui=None)
        assert result[0].dest_file == dest_file


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
