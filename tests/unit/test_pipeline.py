"""Unit tests for pipeline functions: build_cea_performers, build_track_tags, apply_tags_flac, apply_tags_mp3,
find_source_files, check_duration_preflight, _prompt_duration_warnings, run (non-dry-run), and enrich_origin_time.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from mutagen._util import MutagenError
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TXXX  # type: ignore[attr-defined]
from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

import music_annotator
import music_annotator._pipeline_io as _pio
import music_annotator._tags
from music_annotator import (
    JOURNAL_FILENAME,
    CollisionPolicy,
    apply_tags_flac,
    apply_tags_mp3,
    build_cea_performers,
    build_track_tags,
    fetch_acoustid_id,
    find_source_files,
    read_journal,
)
from music_annotator._pipeline import (
    SelectionMethod,
    _apply_collision_suffix,
    _collision_suffix,
    _match_medium_by_title,
    _match_medium_by_toc,
    _prompt_collision_policy,
    _prompt_duration_warnings,
    _resolve_long_names,
    _score_medium_title,
    _select_medium_with_reason,
    _warn_long_names,
    _write_freedb_yaml,
    _write_sidecars,
)
from music_annotator._pipeline_io import (
    _CHROMAPRINT_SIMILARITY_THRESHOLD,
    _DISC_INFO_FILENAME,
    _DISC_TOC_FILENAME,
    PROVENANCE_FILENAME,
    AudioCompareResult,
    _assess_collisions,
    _audio_hash,
    _chromaprint_similarity,
    _collect_work_dir_provenance,
    _find_freedb_sidecar,
    _find_whipper_log,
    _isrc_matches,
    _mtime_iso,
    _parse_ar_track,
    _parse_ar_track_result,
    _read_acoustid_tag,
    _read_albumid_from_tags,
    _read_duration_ms,
    _read_isrc_tag,
    _read_provenance_sidecar,
    _read_tags_flac,
    _read_tags_mp3,
    _run_fpcalc,
    _sha256_file,
    _verify_copy,
    _write_provenance_fields,
    check_duration_preflight,
    compare_audio_collision,
    enrich_origin_time,
    parse_whipper_log,
    rebuild_journal,
)
from music_annotator._tagger import _FLAC_MAX_PICTURE_BYTES
from music_annotator._tags import _NAME_MAX, _work_top_dir, build_dest_path, collect_applied_case_ids
from music_annotator._works import work_group_modal_depth
from music_annotator.models import (
    JSON,
    AccurateRipResult,
    AnnotationTier,
    CensusSignal,
    CopyPlanEntry,
    CoverArt,
    CoverImage,
    MBMedium,
    MBRecording,
    MBRelease,
    MBTrack,
    MBWork,
    ProvenanceSidecar,
    TrackTags,
    TransactionEntry,
    TransactionLog,
    annotation_tier_rank,
)
from tests.conftest import _MINIMAL_FLAC, _MINIMAL_MP3, _rec, _rel, _trk, _w

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

    def test_conductor_includes_chorusmaster_annotation(self) -> None:
        """CONDUCTOR carries chorusmaster annotated as "Name (choirmaster)" after conductors (REND-3/SEL-3 KAT).

        A recording with one conductor and one chorusmaster must produce CONDUCTOR rendered as
        ``"Conductor Name; Chorusmaster Name (choirmaster)"``.  CHORUSMASTER must still carry the bare
        chorusmaster name (additive routing — not a move).  The empty-chorusmasters branch is covered by
        ``test_conductor_populated_from_recording`` (no chorusmaster → CONDUCTOR unchanged).
        """
        recording = _rec(
            {
                "id": "rec-1",
                "title": "Track 1",
                "artist-credit": [],
                "artist-relation-list": [
                    {
                        "type": "conductor",
                        "artist": {"id": "c1", "name": "Herbert von Karajan", "sort-name": "Karajan, Herbert von"},
                        "attribute-list": [],
                    },
                    {
                        "type": "chorus master",
                        "artist": {"id": "cm1", "name": "Simon Halsey", "sort-name": "Halsey, Simon"},
                        "attribute-list": [],
                    },
                ],
                "work-relation-list": [],
            }
        )
        tags = build_track_tags(_make_release(), self._track(), 1, recording, [])
        # KAT 1: CONDUCTOR renders conductor then chorusmaster annotated as "(choirmaster)"
        assert tags.conductor == "Herbert von Karajan; Simon Halsey (choirmaster)"
        # KAT 2: CHORUSMASTER retains the bare chorusmaster name (additive routing, not a move)
        assert tags.chorusmaster == "Simon Halsey"

    def test_conductor_without_chorusmaster_unchanged(self) -> None:
        """CONDUCTOR is exactly the conductor name when no chorusmasters are present (empty-branch coverage).

        This witnesses the empty-chorusmasters branch: the ``if cea.chorusmasters`` guard must leave
        ``conductor_name`` untouched when the list is empty.
        """
        recording = _rec(
            {
                "id": "rec-1",
                "title": "Track 1",
                "artist-credit": [],
                "artist-relation-list": [
                    {
                        "type": "conductor",
                        "artist": {"id": "c1", "name": "Herbert von Karajan", "sort-name": "Karajan, Herbert von"},
                        "attribute-list": [],
                    },
                ],
                "work-relation-list": [],
            }
        )
        tags = build_track_tags(_make_release(), self._track(), 1, recording, [])
        # KAT 3: no chorusmasters → CONDUCTOR is exactly the conductor name, no annotation appended
        assert tags.conductor == "Herbert von Karajan"

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

    def test_cea_recording_artist_billing_order_soloist_conductor_ensemble(self) -> None:
        """CEA_RECORDING_ARTIST renders in billing order: soloists → conductors → ensembles (C-RA-GRAMMAR KAT).

        A recording carrying one soloist, one conductor, and one ensemble must produce exactly
        ``<soloist>; <conductor>; <ensemble>`` — the STYLEGUIDE 4.2 billing order.  A paired assertion
        confirms CEA_MB_ARTISTS carries the raw MB credit phrase, witnessing verbatim-credit separation.

        Note: ``rec_artist_phrase`` derives from the track's recording stub (``track.recording.artist_credit``),
        not from ``recording_detail``.  The artist-credit is therefore set on the track stub here.
        """
        # Track stub carries the verbatim MB credit phrase via its recording.artist-credit.
        track = _trk(
            {
                "id": "trk-1",
                "position": 1,
                "recording": {
                    "id": "rec-1",
                    "title": "Violin Concerto",
                    "artist-credit": [
                        {
                            "name": "MB Credit Phrase",
                            "artist": {"id": "mb1", "name": "MB Credit Phrase", "sort-name": "MB Credit Phrase"},
                        }
                    ],
                },
            }
        )
        recording = _rec(
            {
                "id": "rec-1",
                "title": "Violin Concerto",
                "artist-credit": [],
                "artist-relation-list": [
                    {
                        "type": "performer",
                        "artist": {"id": "s1", "name": "Anne-Sophie Mutter", "sort-name": "Mutter, Anne-Sophie"},
                        "attribute-list": [{"type": "", "value": "violin"}],
                    },
                    {
                        "type": "conductor",
                        "artist": {"id": "c1", "name": "Herbert von Karajan", "sort-name": "Karajan, Herbert von"},
                        "attribute-list": [],
                    },
                    {
                        "type": "performing orchestra",
                        "artist": {
                            "id": "e1",
                            "name": "Berliner Philharmoniker",
                            "sort-name": "Berliner Philharmoniker",
                        },
                        "attribute-list": [],
                    },
                ],
                "work-relation-list": [],
            }
        )
        tags = build_track_tags(_make_release(), track, 1, recording, [])
        # KAT 1: billing order soloists → conductors → ensembles
        assert tags.cea_recording_artist == "Anne-Sophie Mutter; Herbert von Karajan; Berliner Philharmoniker"
        # KAT 2: verbatim MB credit phrase is carried by CEA_MB_ARTISTS, not the assembled composite
        assert tags.cea_mb_artists == "MB Credit Phrase"

    def test_cea_recording_artist_fallback_to_rec_artist_phrase(self) -> None:
        """CEA_RECORDING_ARTIST falls back to rec_artist_phrase when all role classes are empty (C-RA-GRAMMAR KAT).

        With no soloists, conductors, or ensembles on the recording, the assembled composite is empty
        and must fall back to the raw MB recording credit phrase.

        Note: ``rec_artist_phrase`` derives from the track's recording stub (``track.recording.artist_credit``),
        not from ``recording_detail``.  The artist-credit is therefore set on the track stub here.
        """
        # Track stub carries the verbatim MB credit phrase via its recording.artist-credit.
        track = _trk(
            {
                "id": "trk-1",
                "position": 1,
                "recording": {
                    "id": "rec-1",
                    "title": "Track 1",
                    "artist-credit": [
                        {
                            "name": "Fallback Artist",
                            "artist": {"id": "fa1", "name": "Fallback Artist", "sort-name": "Artist, Fallback"},
                        }
                    ],
                },
            }
        )
        recording = _rec(
            {
                "id": "rec-1",
                "title": "Track 1",
                "artist-credit": [],
                "artist-relation-list": [],
                "work-relation-list": [],
            }
        )
        tags = build_track_tags(_make_release(), track, 1, recording, [])
        # KAT 3: empty composite falls back to rec_artist_phrase
        assert tags.cea_recording_artist == "Fallback Artist"


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
        # pylint: disable-next=protected-access
        _write_sidecars(self._make_cover(), work_top, sidecars_written, journal, "now", "rel-1")
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
        # pylint: disable-next=protected-access
        _write_sidecars(self._make_cover(), work_top, sidecars_written, journal, "now", "rel-1")
        first_count = len(journal)
        # pylint: disable-next=protected-access
        _write_sidecars(self._make_cover(), work_top, sidecars_written, journal, "now", "rel-1")
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
        # pylint: disable-next=protected-access
        _write_sidecars(cover, work_top, sidecars_written, journal, "2026-01-01T00:00:00+00:00", "rel-x")

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
        # pylint: disable-next=protected-access
        _write_sidecars(cover, work_top, sidecars_written, journal, "2026-01-01T00:00:00+00:00", "rel-x")

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

    def test_creates_work_top_dir_when_absent(self, fs: FakeFilesystem) -> None:
        """_write_freedb_yaml creates work_top_dir if it does not yet exist.

        When all tracks are skipped (pre-existing), the copy loop never calls mkdir for the work
        directory, so _write_freedb_yaml must create it itself before writing the sidecar.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        work_top = Path("/dest/Artist/Work")
        fs.create_dir(str(src))
        # Deliberately do NOT create work_top — it must be created by _write_freedb_yaml itself.
        fs.create_file(str(src / "00 - disc info.yaml"), contents=self._YAML_CONTENT)
        journal: list[TransactionEntry] = []
        written: set[Path] = set()
        _write_freedb_yaml(src, work_top, 1, written, journal, "now", "rel-1")  # pylint: disable=protected-access
        assert work_top.is_dir()
        assert (work_top / "freedb_disc_1.yaml").exists()
        assert len(journal) == 1


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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            return _rec(
                {"id": rec_id, "title": "Track", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []}
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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
        work_top = _work_top_dir(Path(flac_files[0]), dest)
        freedb_path = work_top / "freedb_disc_1.yaml"
        assert freedb_path.exists()
        # The original disc_id data must be preserved (tier write merges, not replaces).
        freedb_text = freedb_path.read_text(encoding="utf-8")
        assert "disc_id" in freedb_text
        # The annotation tier must also be present (tier write merges into the freedb sidecar).
        assert "annotation_tier" in freedb_text

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

        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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

        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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

        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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

        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            r = _rec({"id": rec_id, "title": "T", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []})
            return r

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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

        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

        captured_dests: list[Path] = []
        real_build = music_annotator._tags.build_dest_path  # pylint: disable=protected-access

        def _capture_dest(
            dest_root: Path,
            rel: MBRelease,
            track: MBTrack,
            tags: TrackTags,
            global_track_idx: int = 0,
            group_modal_depth: int | None = None,
        ) -> Path:
            p = real_build(dest_root, rel, track, tags, global_track_idx, group_modal_depth)
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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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

        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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

        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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

        def _fetch_work(work_id: str, no_cache: bool = False) -> MBWork:  # pylint: disable=unused-argument
            return {"w-mvt1": work_mvt1, "w-mvt2": work_mvt2, top_work_id: work_root}[work_id]

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        # Patch in both locations: _mb_api (used by _get_bottom_work) and _works (used by build_work_hierarchy)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._works.fetch_work_detail", side_effect=_fetch_work)

        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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
                "type": "Classical",  # cwp_worktype_genres_top must contain "Classical" for C-CLASS predicate
                "artist-relation-list": [],
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
            }
        )

        call_count = [0]

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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

        def _fetch_work(work_id: str, no_cache: bool = False) -> MBWork:  # pylint: disable=unused-argument
            return {"w-mvt1": work_mvt1, "w-mvt2": work_mvt2, top_work_id: work_root}[work_id]

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        # Patch in both locations: _mb_api (used by _get_bottom_work) and _works (used by build_work_hierarchy)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._works.fetch_work_detail", side_effect=_fetch_work)

        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

        captured_dests: list[Path] = []
        real_build = music_annotator._tags.build_dest_path  # pylint: disable=protected-access

        def _capture_dest(
            dest_root: Path,
            rel: MBRelease,
            track: MBTrack,
            tags: TrackTags,
            global_track_idx: int = 0,
            group_modal_depth: int | None = None,
        ) -> Path:
            p = real_build(dest_root, rel, track, tags, global_track_idx, group_modal_depth)
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
        # Both movements must share the same top-level directory.
        # C-UNIVERSAL: parts[0] = top_dir (<composer> - <performers>), no class prefix.
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
                "type": "Classical",  # cwp_worktype_genres_top must contain "Classical" for C-CLASS predicate
                "artist-relation-list": [],
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
            }
        )

        call_count = [0]

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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

        def _fetch_work(work_id: str, no_cache: bool = False) -> MBWork:  # pylint: disable=unused-argument
            return {"w-mvt1": work_mvt1, "w-mvt2": work_mvt2, top_work_id: work_root}[work_id]

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        # Patch in both locations: _mb_api (used by _get_bottom_work) and _works (used by build_work_hierarchy)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._works.fetch_work_detail", side_effect=_fetch_work)

        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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

    def test_top_work_groups_span_all_media(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """KAT C-S0: top_work_groups spans all media; copy stays single-medium.

        A 2-medium release where movements of one top work straddle the disc boundary.
        Disc 1 has 2 tracks (movements I and II); disc 2 has 1 track (movement III).
        All three movements share the same top work MBID so they form one group.
        Movement I (disc 1) has recording_date "1981-01-01"; movement III (disc 2) has
        "1984-12-31".  The unified recording_date_work must span both dates, which is only
        possible if the aggregation pass fetched disc 2's recording detail.

        Assertions:
        (a) Both disc-1 tracks receive recording_date_work "1981-01-01/1984-12-31" — the
            cross-medium unified value.  If the aggregation were single-medium only, the
            disc-1-only value would be "1981-01-01" (single date, no range).
        (b) Only the 2 disc-1 files are journalled with action "tagged"; disc 2 is never
            copied or journalled.

        Single-medium regression: for a 1-medium release, fetch_recording_detail is called
        exactly once per track (no extra fetches from the all-media aggregation).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        # --- 2-medium release: disc 1 has 2 tracks, disc 2 has 1 track ---
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        # Source directory contains only disc 1's 2 files.
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "02.flac"), contents=_MINIMAL_FLAC)

        top_work_id = "w-symphony"
        # All three movements share the same top work (w-symphony) via a "parts" backward relation.
        # Movement I (disc 1, track 1): recording_date "1981-01-01" (single date).
        # Movement II (disc 1, track 2): recording_date "1982-06-15" (single date).
        # Movement III (disc 2, track 1): recording_date "1984-12-31" (single date).
        # The unified span across all three is "1981-01-01/1984-12-31".
        # If only disc 1 were aggregated, the span would be "1981-01-01/1982-06-15".

        def _make_mvt_work(work_id: str, title: str) -> MBWork:
            """Build a movement work with a backward 'parts' relation to the top work.

            :param work_id: MBID for this movement work.
            :param title: Title of the movement.
            :returns: An :class:`~music_annotator.models.MBWork` instance.
            """
            return _w(
                {
                    "id": work_id,
                    "title": title,
                    "type": "",
                    "artist-relation-list": [
                        {
                            "type": "composer",
                            "artist": {"id": "a-beethoven", "name": "Beethoven", "sort-name": "Beethoven, Ludwig van"},
                            "attribute-list": [],
                        }
                    ],
                    "work-relation-list": [
                        {"type": "parts", "direction": "backward", "work": {"id": top_work_id, "title": "Symphony"}},
                    ],
                    "attribute-list": [],
                    "tag-list": [],
                }
            )

        work_mvt1 = _make_mvt_work("w-mvt1", "I. Allegro")
        work_mvt2 = _make_mvt_work("w-mvt2", "II. Andante")
        work_mvt3 = _make_mvt_work("w-mvt3", "III. Finale")
        work_root = _w(
            {
                "id": top_work_id,
                "title": "Symphony",
                "type": "Symphony",
                "artist-relation-list": [],
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
            }
        )

        # Build a 2-medium release: disc 1 has 2 tracks, disc 2 has 1 track.
        release = _make_multi_disc_release([2, 1])

        # Map recording IDs to their movement works and session dates.
        # Disc 1: rec-d1-1 → mvt1 (1981-01-01), rec-d1-2 → mvt2 (1982-06-15)
        # Disc 2: rec-d2-1 → mvt3 (1984-12-31)
        rec_to_work: dict[str, MBWork] = {
            "rec-d1-1": work_mvt1,
            "rec-d1-2": work_mvt2,
            "rec-d2-1": work_mvt3,
        }
        rec_to_date: dict[str, str] = {
            "rec-d1-1": "1981-01-01",
            "rec-d1-2": "1982-06-15",
            "rec-d2-1": "1984-12-31",
        }

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            """Return a recording with a performance relation to the appropriate movement work.

            :param rec_id: Recording MBID.
            :returns: An :class:`~music_annotator.models.MBRecording` instance.
            """
            work = rec_to_work[rec_id]
            date = rec_to_date[rec_id]
            return _rec(
                {
                    "id": rec_id,
                    "title": work.title,
                    "artist-credit": [],
                    "artist-relation-list": [
                        {
                            "type": "conductor",
                            "direction": "backward",
                            "begin": date,
                            "end": "",
                            "artist": {"id": "a-cond", "name": "Conductor", "sort-name": "Conductor"},
                        }
                    ],
                    "work-relation-list": [{"type": "performance", "work": {"id": work.id, "title": work.title}}],
                }
            )

        def _fetch_work(work_id: str, no_cache: bool = False) -> MBWork:  # pylint: disable=unused-argument
            """Return the work model for the given MBID.

            :param work_id: Work MBID.
            :returns: An :class:`~music_annotator.models.MBWork` instance.
            """
            return {
                "w-mvt1": work_mvt1,
                "w-mvt2": work_mvt2,
                "w-mvt3": work_mvt3,
                top_work_id: work_root,
            }[work_id]

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        mock_fetch_rec = mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._works.fetch_work_detail", side_effect=_fetch_work)

        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

        music_annotator.run(
            release_id="rel-multi",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        # (a) Both disc-1 tracks must carry the cross-medium unified recording_date_work.
        # The unified value spans all three movements including disc 2's "1984-12-31".
        # If only disc 1 were aggregated, the value would be "1981-01-01/1982-06-15".
        tags1: TrackTags = mock_tag.call_args_list[0][0][1]
        tags2: TrackTags = mock_tag.call_args_list[1][0][1]
        assert tags1.recording_date_work == "1981-01-01/1984-12-31", (
            f"Expected cross-medium unified date '1981-01-01/1984-12-31', got '{tags1.recording_date_work}'. "
            "This indicates the aggregation pass did not span disc 2."
        )
        assert tags2.recording_date_work == "1981-01-01/1984-12-31", (
            f"Expected cross-medium unified date '1981-01-01/1984-12-31', got '{tags2.recording_date_work}'."
        )

        # (b) Only disc-1 files are journalled as "tagged"; disc 2 is never copied.
        # apply_tags_flac must have been called exactly twice (disc 1 only).
        assert mock_tag.call_count == 2, f"Expected 2 tagging calls (disc 1 only), got {mock_tag.call_count}"

        journal_path = dest / JOURNAL_FILENAME
        assert journal_path.exists(), "Journal file must exist after a successful run"
        journal_data = json.loads(journal_path.read_text(encoding="utf-8"))
        tagged_entries = [e for e in journal_data if e["action"] == "tagged"]
        assert len(tagged_entries) == 2, f"Expected 2 'tagged' journal entries (disc 1 only), got {len(tagged_entries)}"

        # Verify fetch_recording_detail was called for all 3 tracks (disc 1 + disc 2).
        assert mock_fetch_rec.call_count == 3, (
            f"Expected 3 fetch_recording_detail calls (all media), got {mock_fetch_rec.call_count}"
        )

    def test_top_work_groups_single_medium_regression(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """KAT C-S0 regression: single-medium release fetches exactly one recording per track.

        For a 1-medium release the all-media set equals the selected-medium set, so no extra
        fetches occur and behaviour is identical to the pre-S0 baseline.

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
        mock_fetch_rec = mocker.patch(
            "music_annotator._pipeline.fetch_recording_detail",
            side_effect=lambda rec_id, no_cache=False: _rec(
                {"id": rec_id, "title": "T", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []}
            ),
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

        # For a single-medium release, fetch count must equal the track count (no extra fetches).
        assert mock_fetch_rec.call_count == 2, (
            f"Expected exactly 2 fetch_recording_detail calls for a 2-track single-medium release, "
            f"got {mock_fetch_rec.call_count}"
        )

    def test_composer_unified_across_media(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """KAT: composer cross-disc fallback propagation spans all media via C-S0 substrate.

        A 2-medium release where movements of one top work straddle the disc boundary.
        Disc 1 (the SELECTED/copied medium) has one movement whose work carries ONLY an
        "additional" composer (Süßmayr — a completion credit).  Disc 2 has one movement whose
        work carries a plain primary composer (Mozart).  Both movements share the same top-work
        MBID.

        Because build_track_tags marks the disc-1 movement ``cwp_composers_is_fallback="1"``
        (it fell back to additional_composers in isolation), the cross-medium composer
        unification pass MUST propagate Mozart's values from disc 2's movement to disc 1's
        movement.  The final ``cwp_composers`` / ``cwp_composer_lastnames`` on the disc-1 track
        must equal Mozart's — a value that is only reachable if disc 2's recording detail was
        fetched and grouped alongside disc 1's during the C-S0 all-media aggregation pass.

        Assertions:
        (a) Disc-1 track receives ``cwp_composers = "Mozart"`` and
            ``cwp_composer_lastnames = "Mozart"`` — propagated from disc 2.
        (b) ``apply_tags_flac`` is called exactly once (disc 1 only; disc 2 is never copied).
        (c) ``fetch_recording_detail`` is called for both recordings (all media).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        # Source dir contains ONLY disc 1's file — disc 2 is never copied.
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        top_work_id = "w-concerto"

        # Disc-1 movement work: ONLY an additional composer (Süßmayr).  In isolation this would
        # set cwp_composers_is_fallback and produce a CWP_COMPOSER_LASTNAMES of "Süßmayr" —
        # a different top_dir than Mozart.  The cross-medium pass must override this.
        work_mvt_d1 = _w(
            {
                "id": "w-mvt-d1",
                "title": "I. Allegro (disc 1)",
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

        # Disc-2 movement work: a plain primary composer (Mozart).  This is the cross-medium
        # source for the propagation.  Its movement is never copied — disc 2 is not in src_dir.
        work_mvt_d2 = _w(
            {
                "id": "w-mvt-d2",
                "title": "II. Rondo (disc 2)",
                "type": "",
                "artist-relation-list": [
                    {
                        "type": "composer",
                        "artist": {
                            "id": "a-mozart",
                            "name": "Mozart",
                            "sort-name": "Mozart, Wolfgang Amadeus",
                        },
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

        # 2-medium release: disc 1 has 1 track, disc 2 has 1 track.
        release = _make_multi_disc_release([1, 1])

        rec_to_work: dict[str, MBWork] = {
            "rec-d1-1": work_mvt_d1,
            "rec-d2-1": work_mvt_d2,
        }
        work_registry: dict[str, MBWork] = {
            "w-mvt-d1": work_mvt_d1,
            "w-mvt-d2": work_mvt_d2,
            top_work_id: work_root,
        }

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            """Return a recording with a performance relation to the appropriate movement work.

            :param rec_id: Recording MBID.
            :returns: An :class:`~music_annotator.models.MBRecording` instance.
            """
            work = rec_to_work[rec_id]
            return _rec(
                {
                    "id": rec_id,
                    "title": work.title,
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [{"type": "performance", "work": {"id": work.id, "title": work.title}}],
                }
            )

        def _fetch_work(work_id: str, no_cache: bool = False) -> MBWork:  # pylint: disable=unused-argument
            """Return the work model for the given MBID.

            :param work_id: Work MBID.
            :returns: An :class:`~music_annotator.models.MBWork` instance.
            """
            return work_registry[work_id]

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        mock_fetch_rec = mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._works.fetch_work_detail", side_effect=_fetch_work)

        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

        music_annotator.run(
            release_id="rel-multi",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        # (a) Disc-1 track must carry Mozart's primary-composer values, propagated cross-medium.
        # Without cross-medium aggregation, disc-1 would have Süßmayr (its own fallback).
        tags_d1: TrackTags = mock_tag.call_args_list[0][0][1]
        assert tags_d1.cwp_composers == "Mozart", (
            f"Expected 'Mozart' (cross-medium propagation from disc 2), got '{tags_d1.cwp_composers}'. "
            "This indicates the composer unification pass did not span disc 2."
        )
        assert tags_d1.cwp_composer_lastnames == "Mozart", (
            f"Expected 'Mozart' for cwp_composer_lastnames, got '{tags_d1.cwp_composer_lastnames}'."
        )

        # (b) apply_tags_flac called exactly once — only disc 1's file is copied and tagged.
        assert mock_tag.call_count == 1, f"Expected 1 tagging call (disc 1 only), got {mock_tag.call_count}"

        # (c) fetch_recording_detail called for both recordings (all-media eagerness).
        assert mock_fetch_rec.call_count == 2, (
            f"Expected 2 fetch_recording_detail calls (disc 1 + disc 2), got {mock_fetch_rec.call_count}"
        )

    def test_recording_first_release_date_unified_across_media(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """KAT: first-release-date [rel YYYY] cross-disc normalisation spans all media.

        A 2-medium release where no movement anywhere has a session date, so the
        ``recording_first_release_date`` normalisation pass (the [rel YYYY] fallback) is the
        active path.  Disc 1 (the SELECTED/copied medium) has one movement with FRD "1963";
        disc 2 has one movement with FRD "1966".  Both movements share the same top-work MBID.
        The release date is "1965".

        The normalisation pass sets every movement's ``recording_first_release_date`` to the
        release year when ``_begins`` is empty (no session dates in the group).  For this to
        run correctly, disc 2 must have been fetched and included in the C-S0 ``tags_map`` /
        ``top_work_groups`` — otherwise the group contains only disc 1 and the disc-2 FRD
        would never be seen.

        The cross-medium proof is structural: we assert that ``fetch_recording_detail`` was
        called for both discs (confirming the substrate ran all-media), and we assert that
        disc 1's ``recording_first_release_date`` is normalised to "1965" (the release year).

        Assertions:
        (a) Disc-1 track's ``recording_first_release_date == "1965"`` (normalised to release year).
        (b) ``apply_tags_flac`` is called exactly once (disc 1 only; disc 2 is never copied).
        (c) ``fetch_recording_detail`` is called for both recordings (all media).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        # Source dir contains ONLY disc 1's file — disc 2 is never copied.
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        top_work_id = "w-symphony"

        def _make_mvt_work(work_id: str, title: str) -> MBWork:
            """Build a movement work with a backward 'parts' relation to the top work.

            :param work_id: MBID for this movement work.
            :param title: Title of the movement.
            :returns: An :class:`~music_annotator.models.MBWork` instance.
            """
            return _w(
                {
                    "id": work_id,
                    "title": title,
                    "type": "",
                    "artist-relation-list": [],
                    "work-relation-list": [
                        {"type": "parts", "direction": "backward", "work": {"id": top_work_id, "title": "Symphony"}},
                    ],
                    "attribute-list": [],
                    "tag-list": [],
                }
            )

        work_mvt_d1 = _make_mvt_work("w-mvt-d1", "I. Allegro (disc 1)")
        work_mvt_d2 = _make_mvt_work("w-mvt-d2", "II. Andante (disc 2)")
        work_root = _w(
            {
                "id": top_work_id,
                "title": "Symphony",
                "type": "Symphony",
                "artist-relation-list": [],
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
            }
        )

        # 2-medium release dated "1965": disc 1 has 1 track, disc 2 has 1 track.
        release = _make_multi_disc_release([1, 1])
        # Override release date so the normalisation has a concrete year to apply.
        release = MBRelease.model_validate(
            {
                "id": "rel-multi-frd",
                "title": "Multi-Disc Symphony",
                "date": "1965",
                "status": "Official",
                "barcode": "",
                "artist-credit": [],
                "release-group": {"id": "rg-sym", "primary-type": "Album", "first-release-date": "1965"},
                "label-info-list": [],
                "text-representation": {"script": "Latn", "language": "eng"},
                "medium-list": [
                    {
                        "position": 1,
                        "format": "CD",
                        "track-list": [
                            {
                                "id": "trk-d1-1",
                                "position": 1,
                                "recording": {"id": "rec-d1-1", "title": "I. Allegro", "artist-credit": []},
                            }
                        ],
                    },
                    {
                        "position": 2,
                        "format": "CD",
                        "track-list": [
                            {
                                "id": "trk-d2-1",
                                "position": 1,
                                "recording": {"id": "rec-d2-1", "title": "II. Andante", "artist-credit": []},
                            }
                        ],
                    },
                ],
            }
        )

        rec_to_work: dict[str, MBWork] = {
            "rec-d1-1": work_mvt_d1,
            "rec-d2-1": work_mvt_d2,
        }
        # No session dates on any movement — ensures _begins stays empty, triggering [rel YYYY].
        rec_to_frd: dict[str, str] = {
            "rec-d1-1": "1963",  # differing FRD on disc 1
            "rec-d2-1": "1966",  # differing FRD on disc 2
        }
        work_registry: dict[str, MBWork] = {
            "w-mvt-d1": work_mvt_d1,
            "w-mvt-d2": work_mvt_d2,
            top_work_id: work_root,
        }

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            """Return a recording with no session date and a per-recording first-release-date.

            :param rec_id: Recording MBID.
            :returns: An :class:`~music_annotator.models.MBRecording` instance.
            """
            work = rec_to_work[rec_id]
            return _rec(
                {
                    "id": rec_id,
                    "title": work.title,
                    "first-release-date": rec_to_frd[rec_id],
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [{"type": "performance", "work": {"id": work.id, "title": work.title}}],
                }
            )

        def _fetch_work(work_id: str, no_cache: bool = False) -> MBWork:  # pylint: disable=unused-argument
            """Return the work model for the given MBID.

            :param work_id: Work MBID.
            :returns: An :class:`~music_annotator.models.MBWork` instance.
            """
            return work_registry[work_id]

        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        mock_fetch_rec = mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._works.fetch_work_detail", side_effect=_fetch_work)

        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

        music_annotator.run(
            release_id="rel-multi-frd",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        # (a) Disc-1 track must have its recording_first_release_date normalised to the release year.
        # The [rel YYYY] normalisation runs because no session dates exist anywhere in the group
        # (_begins is empty).  The normalising source is release.date = "1965".
        tags_d1: TrackTags = mock_tag.call_args_list[0][0][1]
        assert tags_d1.recording_first_release_date == "1965", (
            f"Expected recording_first_release_date normalised to '1965' (release year), "
            f"got '{tags_d1.recording_first_release_date}'. "
            "This indicates the [rel YYYY] normalisation pass did not run correctly."
        )

        # (b) apply_tags_flac called exactly once — only disc 1's file is copied and tagged.
        assert mock_tag.call_count == 1, f"Expected 1 tagging call (disc 1 only), got {mock_tag.call_count}"

        # (c) fetch_recording_detail called for both recordings (all-media eagerness confirms
        # disc 2 was fetched and included in tags_map / top_work_groups for the normalisation pass).
        assert mock_fetch_rec.call_count == 2, (
            f"Expected 2 fetch_recording_detail calls (disc 1 + disc 2), got {mock_fetch_rec.call_count}"
        )

    def test_cwp_worktype_genres_top_populated(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """KAT: cwp_worktype_genres_top carries the top work's type; cwp_worktype_genres carries the bottom.

        A concerto-movement hierarchy: the bottom (movement) work has type "" while only the root
        work carries type "Concerto".  build_cwp_tags must set cwp_worktype_genres_top to "Concerto"
        (work_hierarchy[-1].type) and leave cwp_worktype_genres as "" (work_hierarchy[0].type).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=1)
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        top_work_id = "w-concerto"
        # Movement work: type is empty — only the root carries "Concerto".
        work_movement = _w(
            {
                "id": "w-mvt1",
                "title": "I. Allegro",
                "type": "",
                "artist-relation-list": [
                    {
                        "type": "composer",
                        "artist": {"id": "a-brahms", "name": "Brahms", "sort-name": "Brahms, Johannes"},
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
        work_root = _w(
            {
                "id": top_work_id,
                "title": "Concerto for Violin",
                "type": "Concerto",
                "artist-relation-list": [],
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
            }
        )

        def _fetch_work(work_id: str, no_cache: bool = False) -> MBWork:  # pylint: disable=unused-argument
            """Return the work model for the given MBID.

            :param work_id: Work MBID.
            :returns: An :class:`~music_annotator.models.MBWork` instance.
            """
            return {top_work_id: work_root, "w-mvt1": work_movement}[work_id]

        mocker.patch(
            "music_annotator._pipeline.fetch_recording_detail",
            return_value=_rec(
                {
                    "id": "rec-1",
                    "title": "I. Allegro",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [{"type": "performance", "work": {"id": "w-mvt1", "title": "I. Allegro"}}],
                }
            ),
        )
        mocker.patch("music_annotator._mb_api.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._works.fetch_work_detail", side_effect=_fetch_work)

        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        tags: TrackTags = mock_tag.call_args_list[0][0][1]
        # Top work carries "Concerto"; bottom (movement) work's type is "".
        assert tags.cwp_worktype_genres_top == "Concerto", (
            f"Expected cwp_worktype_genres_top='Concerto' (top work type), got '{tags.cwp_worktype_genres_top}'"
        )
        assert tags.cwp_worktype_genres == "", (
            f"Expected cwp_worktype_genres='' (movement work type), got '{tags.cwp_worktype_genres}'"
        )


# ---------------------------------------------------------------------------
# KAT C-L1 — intermediate sibling index substrate (run() enumeration pass)
# ---------------------------------------------------------------------------


class TestIntermediateSiblingIndexSubstrate:
    """KAT C-L1: run() assigns gap-free cwp_inter_index_{i} for intermediate hierarchy nodes.

    Exercises the enumeration pass added to _pipeline.py: for each top-work group, for each
    intermediate level i >= 1, distinct sibling nodes are ranked by ascending
    cwp_ordering_key_{i}, and the resulting gap-free 1-based index is stored as
    cwp_inter_index_{i} on every track of that node.
    """

    def _patch_mb(self, mocker: MockerFixture, release: MBRelease) -> None:
        """Patch all MB API calls and post-copy verification.

        :param mocker: pytest-mock fixture.
        :param release: MBRelease to return from fetch_release.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

    def test_inter_index_gap_free_for_non_contiguous_ordering_keys(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """cwp_inter_index_1 is gap-free (1, 2) even when ordering-keys are non-contiguous (2, 5).

        KAT for C-L1 substrate: a 3-track opera release where tracks 1-2 share Act I
        (ordering-key=2) and track 3 belongs to Act II (ordering-key=5).  The pipeline
        enumeration pass must assign cwp_inter_index_1="1" to Act I tracks and
        cwp_inter_index_1="2" to the Act II track.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "02.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "03.flac"), contents=_MINIMAL_FLAC)

        opera_id = "w-opera"
        act1_id = "w-act1"
        act2_id = "w-act2"

        # Opera (top work) — no composer, no backward parent relation.
        opera_work = _w(
            {
                "id": opera_id,
                "title": "Die Walküre",
                "type": "Opera",
                "artist-relation-list": [],
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
            }
        )

        def _make_act(act_id: str, title: str, ordering_key: str) -> MBWork:
            """Build an act work with a backward parts relation to the opera.

            :param act_id: MBID for this act.
            :param title: Title of the act.
            :param ordering_key: MB ordering-key in the parts/backward relation to the opera.
            :returns: An :class:`~music_annotator.models.MBWork` instance.
            """
            return _w(
                {
                    "id": act_id,
                    "title": title,
                    "type": "",
                    "artist-relation-list": [],
                    "work-relation-list": [
                        {
                            "type": "parts",
                            "direction": "backward",
                            "ordering-key": ordering_key,
                            "work": {"id": opera_id, "title": "Die Walküre"},
                        }
                    ],
                    "attribute-list": [],
                    "tag-list": [],
                }
            )

        act1_work = _make_act(act1_id, "Akt I", ordering_key="2")
        act2_work = _make_act(act2_id, "Akt II", ordering_key="5")

        def _make_aria(aria_id: str, act_id: str, act_title: str, ordering_key: str) -> MBWork:
            """Build an aria work with a backward parts relation to its act.

            :param aria_id: MBID for this aria.
            :param act_id: MBID of the parent act.
            :param act_title: Title of the parent act.
            :param ordering_key: MB ordering-key in the parts/backward relation to the act.
            :returns: An :class:`~music_annotator.models.MBWork` instance.
            """
            return _w(
                {
                    "id": aria_id,
                    "title": f"Aria {aria_id}",
                    "type": "",
                    "artist-relation-list": [],
                    "work-relation-list": [
                        {
                            "type": "parts",
                            "direction": "backward",
                            "ordering-key": ordering_key,
                            "work": {"id": act_id, "title": act_title},
                        }
                    ],
                    "attribute-list": [],
                    "tag-list": [],
                }
            )

        # Two arias in Act I (ordering-keys 1 and 2 within the act) and one aria in Act II.
        aria1 = _make_aria("w-aria1", act1_id, "Akt I", ordering_key="1")
        aria2 = _make_aria("w-aria2", act1_id, "Akt I", ordering_key="2")
        aria3 = _make_aria("w-aria3", act2_id, "Akt II", ordering_key="1")

        # 3-track single-disc release.
        release = _make_release(n_tracks=3)

        # Map recording IDs to aria works.
        rec_to_aria: dict[str, MBWork] = {
            "rec-1": aria1,
            "rec-2": aria2,
            "rec-3": aria3,
        }
        # Map work IDs to full work objects (for fetch_work_detail).
        work_registry: dict[str, MBWork] = {
            "w-aria1": aria1,
            "w-aria2": aria2,
            "w-aria3": aria3,
            act1_id: act1_work,
            act2_id: act2_work,
            opera_id: opera_work,
        }

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            """Return a recording with a performance relation to the appropriate aria work.

            :param rec_id: Recording MBID.
            :returns: An :class:`~music_annotator.models.MBRecording` instance.
            """
            aria = rec_to_aria[rec_id]
            return _rec(
                {
                    "id": rec_id,
                    "title": aria.title,
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [{"type": "performance", "work": {"id": aria.id, "title": aria.title}}],
                }
            )

        def _fetch_work(work_id: str, no_cache: bool = False) -> MBWork:  # pylint: disable=unused-argument
            """Return the work model for the given MBID.

            :param work_id: Work MBID.
            :returns: An :class:`~music_annotator.models.MBWork` instance.
            """
            return work_registry[work_id]

        self._patch_mb(mocker, release)
        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._works.fetch_work_detail", side_effect=_fetch_work)
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        assert mock_tag.call_count == 3
        tags1: TrackTags = mock_tag.call_args_list[0][0][1]
        tags2: TrackTags = mock_tag.call_args_list[1][0][1]
        tags3: TrackTags = mock_tag.call_args_list[2][0][1]

        # Tracks 1 and 2 belong to Act I (ordering-key=2 → sibling index=1 gap-free).
        # Track 3 belongs to Act II (ordering-key=5 → sibling index=2 gap-free).
        extras1 = tags1.model_extra or {}
        extras2 = tags2.model_extra or {}
        extras3 = tags3.model_extra or {}
        assert extras1.get("cwp_inter_index_1") == "1", (
            f"Track 1 (Act I) expected cwp_inter_index_1='1', got {extras1.get('cwp_inter_index_1')!r}. "
            "Non-contiguous ordering-key=2 must not propagate to the gap-free sibling index."
        )
        assert extras2.get("cwp_inter_index_1") == "1", (
            f"Track 2 (Act I) expected cwp_inter_index_1='1', got {extras2.get('cwp_inter_index_1')!r}"
        )
        assert extras3.get("cwp_inter_index_1") == "2", (
            f"Track 3 (Act II) expected cwp_inter_index_1='2', got {extras3.get('cwp_inter_index_1')!r}. "
            "Non-contiguous ordering-key=5 must map to gap-free index=2 (not 5)."
        )

    def test_inter_index_skips_tracks_without_intermediate_level(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Tracks lacking cwp_workid_{i} in their extras are skipped during enumeration.

        Exercises the empty-node-id guards in the intermediate sibling index pass:
        - collection loop: ``if not node_id: continue`` (no cwp_workid_i present on this track)
        - write-back loop: ``if node_id:`` False branch (track has no intermediate at level i)

        Scenario: a 2-track group in the same top-work (opera) where track 1 is a 3-level
        (aria→act→opera, cwp_workid_0/1/2 all set) and track 2 is a 2-level (movement→opera,
        cwp_workid_0/1 set, no cwp_workid_2).  When the enumeration pass processes level 2
        (because max_inter_level=2 from track 1), track 2 produces an empty node_id at level 2
        → it is correctly skipped in both loops.  Track 2 gets cwp_inter_index_1 (level 1, both
        tracks have cwp_workid_1) but must NOT get cwp_inter_index_2 (only track 1 has
        cwp_workid_2).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "02.flac"), contents=_MINIMAL_FLAC)

        opera_id = "w-opera-mixed"
        act1_id = "w-act1-mixed"

        opera_work = _w(
            {
                "id": opera_id,
                "title": "Mixed Opera",
                "type": "Opera",
                "artist-relation-list": [],
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
            }
        )
        act1_work = _w(
            {
                "id": act1_id,
                "title": "Akt I",
                "type": "",
                "artist-relation-list": [],
                "work-relation-list": [
                    {
                        "type": "parts",
                        "direction": "backward",
                        "ordering-key": "1",
                        "work": {"id": opera_id, "title": "Mixed Opera"},
                    }
                ],
                "attribute-list": [],
                "tag-list": [],
            }
        )
        # Track 1: 3-level — aria → Akt I → opera
        aria1 = _w(
            {
                "id": "w-aria1-mixed",
                "title": "Aria 1",
                "type": "",
                "artist-relation-list": [],
                "work-relation-list": [
                    {
                        "type": "parts",
                        "direction": "backward",
                        "ordering-key": "1",
                        "work": {"id": act1_id, "title": "Akt I"},
                    }
                ],
                "attribute-list": [],
                "tag-list": [],
            }
        )
        # Track 2: 2-level — directly under the opera (no intermediate act).
        # A direct backward-parts link to the opera gives part_levels=1.
        direct_mvt = _w(
            {
                "id": "w-direct-mixed",
                "title": "Direct Movement",
                "type": "",
                "artist-relation-list": [],
                "work-relation-list": [
                    {
                        "type": "parts",
                        "direction": "backward",
                        "ordering-key": "2",
                        "work": {"id": opera_id, "title": "Mixed Opera"},
                    }
                ],
                "attribute-list": [],
                "tag-list": [],
            }
        )

        release = _make_release(n_tracks=2)

        rec_to_aria: dict[str, MBWork] = {
            "rec-1": aria1,
            "rec-2": direct_mvt,
        }
        work_registry: dict[str, MBWork] = {
            "w-aria1-mixed": aria1,
            "w-direct-mixed": direct_mvt,
            act1_id: act1_work,
            opera_id: opera_work,
        }

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            """Return a recording with a performance relation to the appropriate work.

            :param rec_id: Recording MBID.
            :returns: An :class:`~music_annotator.models.MBRecording` instance.
            """
            work = rec_to_aria[rec_id]
            return _rec(
                {
                    "id": rec_id,
                    "title": work.title,
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [{"type": "performance", "work": {"id": work.id, "title": work.title}}],
                }
            )

        def _fetch_work(work_id: str, no_cache: bool = False) -> MBWork:  # pylint: disable=unused-argument
            """Return the work model for the given MBID.

            :param work_id: Work MBID.
            :returns: An :class:`~music_annotator.models.MBWork` instance.
            """
            return work_registry[work_id]

        self._patch_mb(mocker, release)
        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._works.fetch_work_detail", side_effect=_fetch_work)
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
        tags1: TrackTags = mock_tag.call_args_list[0][0][1]
        tags2: TrackTags = mock_tag.call_args_list[1][0][1]

        extras1 = tags1.model_extra or {}
        extras2 = tags2.model_extra or {}

        # Track 1 (3-level: aria→Akt I→opera) has cwp_workid_0/1/2 set.
        # max_inter_level=2; the enumeration processes i=1 and i=2.
        # At i=1: track 1's node_id = act1_id (Akt I); gets cwp_inter_index_1="1".
        assert extras1.get("cwp_inter_index_1") == "1", (
            f"Track 1 (3-level via Akt I) expected cwp_inter_index_1='1', got {extras1.get('cwp_inter_index_1')!r}"
        )
        # At i=2: track 1's node_id = opera_id; gets cwp_inter_index_2="1".
        assert extras1.get("cwp_inter_index_2") == "1", (
            f"Track 1 (3-level) expected cwp_inter_index_2='1', got {extras1.get('cwp_inter_index_2')!r}"
        )

        # Track 2 (2-level: movement→opera) has cwp_workid_0/1 set but NO cwp_workid_2.
        # At i=1: track 2's cwp_workid_1 = opera_id → non-empty node_id → gets cwp_inter_index_1.
        assert "cwp_inter_index_1" in extras2, "Track 2 (2-level) must receive cwp_inter_index_1 (its cwp_workid_1 is opera_id)"
        # At i=2: track 2 has NO cwp_workid_2 → empty node_id → the 'if not node_id: continue'
        # guard fires in the collection loop; the 'if node_id:' False branch fires in the
        # write-back loop.  Track 2 must NOT receive cwp_inter_index_2.
        assert "cwp_inter_index_2" not in extras2, (
            f"Track 2 (2-level, no cwp_workid_2) must not receive cwp_inter_index_2, "
            f"but got {extras2.get('cwp_inter_index_2')!r}"
        )


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
# build_track_tags — IS_CLASSICAL from work-type predicate (REND-21/SEL-14)
# ---------------------------------------------------------------------------


class TestBuildTrackTagsIsClassical:
    """KATs (c): IS_CLASSICAL-from-work-type witnesses (REND-21/SEL-14).

    IS_CLASSICAL derives from compositional identity: the CE-classical predicate is
    ``cwp_work_top`` non-empty AND ``cwp_worktype_genres_top`` contains ``"Classical"``.
    Tag layer ≠ path layer: this flag is independent of any path component.
    Both branches (classical → "1"; non-classical → "0") are covered.
    """

    def test_is_classical_one_for_classical_release(self) -> None:
        """KAT REND-21a: IS_CLASSICAL is "1" when the CE-classical predicate is satisfied.

        A work hierarchy whose top work has type "Classical" satisfies the CE-classical predicate
        (cwp_work_top non-empty AND cwp_worktype_genres_top contains "Classical"), so
        IS_CLASSICAL must be "1".  Independent of any path component.
        """
        work = _w(
            {
                "id": "w-sym",
                "title": "Symphony No. 5",
                "type": "Classical",
                "artist-relation-list": [
                    {
                        "type": "composer",
                        "artist": {"id": "a-beethoven", "name": "Beethoven", "sort-name": "Beethoven, Ludwig van"},
                        "attribute-list": [],
                    }
                ],
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
            }
        )
        rec = _rec(
            {
                "id": "rec-1",
                "title": "Symphony No. 5",
                "artist-credit": [],
                "artist-relation-list": [],
                "work-relation-list": [{"type": "performance", "work": {"id": "w-sym", "title": "Symphony No. 5"}}],
            }
        )
        track = _trk({"id": "t1", "position": 1, "recording": {"id": "rec-1", "title": "Symphony No. 5", "artist-credit": []}})
        tags = build_track_tags(_make_release(), track, 1, rec, [work])
        assert tags.is_classical == "1", f"Expected IS_CLASSICAL='1' for classical release, got '{tags.is_classical}'"

    def test_is_classical_zero_for_non_classical_release(self) -> None:
        """KAT REND-21b: IS_CLASSICAL is "0" when the CE-classical predicate is not satisfied.

        A release with no work link (cwp_work_top empty) does not satisfy the CE-classical
        predicate, so IS_CLASSICAL must be "0".  The flag derives from compositional identity,
        not from any path component.
        """
        rec = _rec(
            {
                "id": "rec-pop",
                "title": "Pop Track",
                "artist-credit": [],
                "artist-relation-list": [],
                "work-relation-list": [],  # no work link → cwp_work_top empty → predicate false
            }
        )
        track = _trk({"id": "t1", "position": 1, "recording": {"id": "rec-pop", "title": "Pop Track", "artist-credit": []}})
        tags = build_track_tags(
            _make_release(),
            track,
            1,
            rec,
            [],  # empty work hierarchy → cwp_work_top empty → IS_CLASSICAL "0"
        )
        assert tags.is_classical == "0", f"Expected IS_CLASSICAL='0' for non-classical release, got '{tags.is_classical}'"

    def test_is_classical_one_independent_of_path(self) -> None:
        """KAT REND-21c: IS_CLASSICAL is "1" for a classical work even though the path is prefix-less.

        Proves the decouple: a classical work whose path has no class prefix still gets
        IS_CLASSICAL == "1" because the flag derives from the work-type predicate, not the path.
        """
        work = _w(
            {
                "id": "w-sonata",
                "title": "Sonata in B minor",
                "type": "Classical",
                "artist-relation-list": [
                    {
                        "type": "composer",
                        "artist": {"id": "a-schubert", "name": "Schubert", "sort-name": "Schubert, Franz"},
                        "attribute-list": [],
                    }
                ],
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
            }
        )
        rec = _rec(
            {
                "id": "rec-sonata",
                "title": "Sonata in B minor",
                "artist-credit": [],
                "artist-relation-list": [],
                "work-relation-list": [{"type": "performance", "work": {"id": "w-sonata", "title": "Sonata in B minor"}}],
            }
        )
        track = _trk(
            {"id": "t1", "position": 1, "recording": {"id": "rec-sonata", "title": "Sonata in B minor", "artist-credit": []}}
        )
        tags = build_track_tags(_make_release(), track, 1, rec, [work])
        # The path is prefix-less (C-UNIVERSAL) — no "Classical" directory component.
        # IS_CLASSICAL must still be "1" because it derives from the work-type predicate.
        assert tags.is_classical == "1", (
            f"Expected IS_CLASSICAL='1' for classical work with prefix-less path, got '{tags.is_classical}'"
        )
        # Verify the path is indeed prefix-less (no "Classical" class component).
        assert tags.cwp_work_top == "Sonata in B minor", "cwp_work_top must be set for the predicate to fire"
        assert "Classical" in tags.cwp_worktype_genres_top, "cwp_worktype_genres_top must contain 'Classical'"


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
        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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

        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        mock_work.assert_called_once_with("w1", no_cache=False)

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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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

        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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

        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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
        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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

        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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
        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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

        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        # fetch_work_detail MUST be called — work had no inlined relation data
        mock_work.assert_called_once_with("w1", no_cache=False)

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
        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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
                "type": "Classical",  # cwp_worktype_genres_top must contain "Classical" for C-CLASS predicate
                "title": "Violin Concerto in D major, Op. 61",
                "work-relation-list": [],
                "artist-relation-list": [
                    {"type": "composer", "artist": {"id": "a-beet", "name": "Beethoven", "sort-name": "Beethoven, Ludwig van"}},
                ],
                "attribute-list": [],
                "tag-list": [],
            }
        )

        def _fetch_work(work_id: str, no_cache: bool = False) -> MBWork:  # pylint: disable=unused-argument
            return {"w-cad": cadenza_work, "w-mvt": concerto_mvt, "w-conc": concerto_root}[work_id]

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        # Patch in both locations: _mb_api (used by _get_bottom_work) and _works (used by select_primary_performance_work)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._works.fetch_work_detail", side_effect=_fetch_work)
        mocker.patch("music_annotator._pipeline.apply_tags_flac")

        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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

        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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

    def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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
    mocker.patch("music_annotator._pipeline.apply_tags_flac")
    mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
    mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")


class TestPromptCollisionPolicy:
    """Tests for _prompt_collision_policy."""

    def _make_result(self, dest: Path, match: bool | None = None) -> AudioCompareResult:
        """Build an AudioCompareResult for a collision path, defaulting to inconclusive.

        :param dest: The destination path that collides.
        :param match: ``True``, ``False``, or ``None`` (default).
        :returns: An :class:`~music_annotator._pipeline_io.AudioCompareResult`.
        """
        return AudioCompareResult(
            src=Path("/src/dummy.flac"),
            dest=dest,
            match=match,
            method="unknown",
            detail="test fixture",
        )

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
        results = [
            self._make_result(work_dir / "01 - Symphony no. 2 in D major, op. 73_ I.flac"),
            self._make_result(work_dir / "02 - Symphony no. 2 in D major, op. 73_ II.flac"),
        ]
        printed: list[str] = []
        mocker.patch("music_annotator._pipeline._console.print", side_effect=lambda s, **_: printed.append(s))
        mocker.patch("builtins.input", return_value="s")

        _prompt_collision_policy(results, dest)  # pylint: disable=protected-access

        # The work-top-dir with the date suffix must appear in the directory summary line.
        # Path strings are passed through rich.markup.escape, so brackets become \[…].
        assert any("Sinfonie Nr. 2 D-Dur, op. 73 \\[rec 1977-1978]" in line for line in printed)
        # The absolute dest prefix must NOT appear in the directory lines (relative paths only).
        assert not any(str(dest) in line and "Sinfonie" in line for line in printed)
        # Both individual filenames must appear in the flat filename list (as relative paths).
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
        results = [self._make_result(w1 / "01.flac"), self._make_result(w1 / "02.flac"), self._make_result(w2 / "01.flac")]
        printed: list[str] = []
        mocker.patch("music_annotator._pipeline._console.print", side_effect=lambda s, **_: printed.append(s))
        mocker.patch("builtins.input", return_value="s")

        _prompt_collision_policy(results, dest)  # pylint: disable=protected-access

        # Path strings are passed through rich.markup.escape, so brackets become \[…].
        assert any("Sinfonie Nr. 1 c-Moll, op. 68 \\[rec 1977-1978]" in line for line in printed)
        assert any("Sinfonie Nr. 3 F-Dur, op. 90 \\[rec 1977-1978]" in line for line in printed)
        # The work-dir grouping summary should list each work dir exactly once (not once per file).
        # Work-dir summary lines contain "Sinfonie" but no ".flac" extension anywhere in the line;
        # per-file lines contain ".flac" (with appended comparison context after the path).
        work_dir_lines = [line for line in printed if "Sinfonie" in line and ".flac" not in line]
        assert len(work_dir_lines) == 2

    def test_rich_escape_applied_to_dest_root_in_warning(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_prompt_collision_policy escapes dest_root so [rel YYYY] brackets are not eaten by Rich.

        Rich interprets ``[rel 1999]`` as an unknown markup tag and silently discards it.
        The fix wraps every path interpolated into a Rich format string with
        ``rich.markup.escape``.  This test verifies the escape is applied by patching the escape
        function and asserting it was called with the dest_root string.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest = Path("/dest [rel 1999]")
        work_dir = dest / "Wagner - Karajan" / "Die Meistersinger [rel 1999]"
        fs.create_dir(str(work_dir))
        results = [self._make_result(work_dir / "03 - Scene III.flac")]
        mocker.patch("music_annotator._pipeline._console.print")
        mocker.patch("builtins.input", return_value="s")
        mock_escape = mocker.patch("music_annotator._pipeline._markup_escape", side_effect=lambda s: s)

        _prompt_collision_policy(results, dest)  # pylint: disable=protected-access

        escaped_strings = [call.args[0] for call in mock_escape.call_args_list]
        assert str(dest) in escaped_strings

    def test_rich_escape_applied_to_work_dir_in_warning(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Work-dir path in collision warning is Rich-escaped so [rec YYYY] suffix is not stripped.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest = Path("/dest")
        work_dir = dest / "Wagner - Karajan" / "Die Meistersinger [rel 1999]"
        fs.create_dir(str(work_dir))
        results = [self._make_result(work_dir / "03 - Scene III.flac")]
        mocker.patch("music_annotator._pipeline._console.print")
        mocker.patch("builtins.input", return_value="s")
        mock_escape = mocker.patch("music_annotator._pipeline._markup_escape", side_effect=lambda s: s)

        _prompt_collision_policy(results, dest)  # pylint: disable=protected-access

        escaped_strings = [call.args[0] for call in mock_escape.call_args_list]
        # The work-dir relative path ("Wagner - Karajan/Die Meistersinger [rel 1999]") must be escaped.
        assert any("Die Meistersinger [rel 1999]" in s for s in escaped_strings)

    def test_conflicting_files_shown_as_relative_path_not_basename(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Individual conflicting files are listed as relative paths, not just basenames.

        For a 3-level opera path (…/02 - Akt I/03 - Scene.flac) the intermediate Act subdir
        must appear in the per-file display so the user can distinguish which Act each file
        belongs to.  Previously p.name was used, hiding the subdirectory context.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest = Path("/dest")
        w1 = dest / "Wagner - Karajan" / "Meistersinger [rel 1999]" / "02 - Akt I"
        w2 = dest / "Wagner - Karajan" / "Meistersinger [rel 1999]" / "03 - Akt II"
        fs.create_dir(str(w1))
        fs.create_dir(str(w2))
        results = [
            self._make_result(w1 / "03 - Scene III.flac"),
            self._make_result(w2 / "01 - Scene I.flac"),
        ]
        printed: list[str] = []
        mocker.patch("music_annotator._pipeline._console.print", side_effect=lambda s, **_: printed.append(s))
        mocker.patch("music_annotator._pipeline._markup_escape", side_effect=lambda s: s)
        mocker.patch("builtins.input", return_value="s")

        _prompt_collision_policy(results, dest)  # pylint: disable=protected-access

        # The intermediate Act directory must appear in the per-file listing.
        assert any("02 - Akt I" in line and "03 - Scene III.flac" in line for line in printed)
        assert any("03 - Akt II" in line and "01 - Scene I.flac" in line for line in printed)
        # Bare basenames without subdirectory context must NOT appear as standalone entries.
        assert not any(line.strip().endswith("03 - Scene III.flac") and "Akt" not in line for line in printed)


def test_no_dd_suffix_on_distinct_titles() -> None:
    """KAT L3: split-work recordings with distinct CWP_MOVT_NUM produce no .dd filenames.

    Exercises the absence of the retired _dedup_plan_entries pass.  Before L0/L3, several
    recordings sharing the same MB bottom-work ordering-key (e.g. all cwp_ordering_key_0="1")
    would produce identical destination paths, which _dedup_plan_entries would then rename using
    the ``{ok}.{idx:02d}`` compound prefix (e.g. "01.01 - …", "01.02 - …").  With C-L0 in place,
    build_dest_path reads CWP_MOVT_NUM — the per-group, gap-free index — so each recording
    already gets a unique leaf, and the dedup pass is not needed.

    This test constructs a Mahler-9-first-movement-shaped scenario: three recordings that all
    share cwp_ordering_key_0="1" (the same MB bottom work) but carry distinct CWP_MOVT_NUM
    values (1, 2, 3) from the pipeline's top-work-group enumeration.  It asserts:

    - All three destination paths are unique (no collision).
    - No path component contains ".dd" (the sentinel the old dedup suffix would have produced
      under the legacy naming scheme, which prefixed deduped stems with "01.dd" etc.).
    - No path component matches the old compound-prefix pattern ``\\d+\\.\\d{2}`` (e.g. "01.01").
    """
    dest_root = Path("/lib")
    release = _rel({"id": "r1", "title": "Symphony No. 9", "artist-credit": [], "medium-list": []})
    track = _trk({"id": "t1", "position": 1, "recording": {"id": "r1", "title": "Movement I — Andante"}})

    def _make_split_tags(movt_num: str) -> TrackTags:
        """Build TrackTags for a 2-level split-work movement sharing one MB bottom work.

        All three recordings share cwp_ordering_key_0="1" (the Mahler-9-mvt-I bug case).
        The per-group index CWP_MOVT_NUM differs per recording — this is the C-L0 authority.

        :param movt_num: Per-group track index assigned by the top-work-group enumeration pass.
        :returns: A :class:`~music_annotator.models.TrackTags` instance.
        """
        tags = TrackTags(
            title="Andante comodo",
            movementnumber=movt_num,
            movementtotal="3",
            cwp_work_top="Symphony No. 9",
            cwp_composer_lastnames="Mahler",
            originaldate="1998",
            cwp_part_levels="1",
            cwp_movt_num=movt_num,
            cea_conductors_list=[],
            cea_ensembles_list=[],
        )
        # Simulate the old bug source: all recordings share the same bottom-work ordering-key.
        # With _dedup_plan_entries removed, this must NOT cause any .dd renaming.
        tags.model_extra["cwp_ordering_key_0"] = "1"  # type: ignore[index]
        return tags

    leaves = [
        music_annotator._tags.build_dest_path(  # pylint: disable=protected-access
            dest_root, release, track, _make_split_tags(n)
        ).name
        for n in ("1", "2", "3")
    ]

    # All three paths must be distinct — CWP_MOVT_NUM gives a unique leaf for each recording.
    assert len(set(leaves)) == 3, f"Expected 3 distinct leaves, got duplicates: {leaves}"

    # No .dd substring anywhere (the sentinel the old dedup pass would have introduced).
    assert not any(".dd" in leaf for leaf in leaves), (
        f"Found .dd suffix in leaves — dead dedup pass must have been reinstated: {leaves}"
    )

    # No compound-prefix pattern like "01.01" (the {ok}.{idx:02d} shape from _dedup_plan_entries).
    _compound_prefix = re.compile(r"^\d+\.\d{2}\b")
    assert not any(_compound_prefix.match(leaf) for leaf in leaves), (
        f"Found compound dedup prefix in leaves — dead dedup pass must have been reinstated: {leaves}"
    )

    # Positive assertion: leaves are the 01/02/03 sequential form driven by CWP_MOVT_NUM.
    assert leaves[0].startswith("01 - "), f"Expected leaf 1 to start with '01 - ', got: {leaves[0]!r}"
    assert leaves[1].startswith("02 - "), f"Expected leaf 2 to start with '02 - ', got: {leaves[1]!r}"
    assert leaves[2].startswith("03 - "), f"Expected leaf 3 to start with '03 - ', got: {leaves[2]!r}"


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

    def _make_collision_result(self, dest: Path, collision_path: Path, match: bool | None = None) -> AudioCompareResult:
        """Build an AudioCompareResult representing an inconclusive collision at ``collision_path``.

        :param dest: Dummy source path.
        :param collision_path: The existing destination path that collides.
        :param match: Comparison outcome (default ``None`` = inconclusive).
        :returns: An :class:`~music_annotator._pipeline_io.AudioCompareResult`.
        """
        return AudioCompareResult(
            src=dest / "dummy_src.flac",
            dest=collision_path,
            match=match,
            method="unknown",
            detail="test fixture",
        )

    def test_collision_overwrite_copies_file(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Choosing 'overwrite' when a collision exists still copies and tags the file.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        _setup_single_track_run(mocker, fs, src, dest)

        # Pre-populate the destination with any file to create a guaranteed collision.
        # We patch _assess_collisions to return a fixed AudioCompareResult (inconclusive) so we
        # don't depend on the exact dest path that build_dest_path would compute.
        # Path must be at least 2 levels deep relative to dest_root so _prompt_collision_policy
        # can extract parts[0]/parts[1] for the work-dir display.
        collision_path = dest / "Composer - Performer" / "Work [rec 1970]" / "existing.flac"
        fs.create_file(str(collision_path))
        collision_result = self._make_collision_result(dest, collision_path)
        mocker.patch("music_annotator._pipeline._assess_collisions", return_value=[collision_result])
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
        collision_result = self._make_collision_result(dest, collision_path)
        mocker.patch("music_annotator._pipeline._assess_collisions", return_value=[collision_result])
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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            return _rec(
                {"id": rec_id, "title": "Track", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []}
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

        # We'll intercept _assess_collisions to report the first dest as an inconclusive collision.
        captured_dests: list[Path] = []

        def _capture_assess(pairs: list[tuple[Path, Path, str, int]]) -> list[AudioCompareResult]:
            for _src, dest_p, _acoustid, _len in pairs:
                captured_dests.append(dest_p)
            first_src, first_dest, _, _ = pairs[0]
            return [AudioCompareResult(src=first_src, dest=first_dest, match=None, method="unknown", detail="test")]

        mocker.patch(  # pylint: disable=protected-access
            "music_annotator._pipeline._assess_collisions", side_effect=_capture_assess
        )
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
        collision_result = self._make_collision_result(dest, collision_path)
        mocker.patch("music_annotator._pipeline._assess_collisions", return_value=[collision_result])
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
        collision_result = self._make_collision_result(dest, collision_path)
        mocker.patch("music_annotator._pipeline._assess_collisions", return_value=[collision_result])
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
        collision_result = self._make_collision_result(dest, collision_path)
        mocker.patch("music_annotator._pipeline._assess_collisions", return_value=[collision_result])
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
        mocker.patch("music_annotator._pipeline._assess_collisions", return_value=[])  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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
        collision_result = self._make_collision_result(dest, collision_path)
        mocker.patch("music_annotator._pipeline._assess_collisions", return_value=[collision_result])
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
        # pylint: disable-next=protected-access
        writable = music_annotator._tagger._MP3_STD_KEYS | frozenset(music_annotator._tagger._MP3_TXXX_MAP)
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
        mocker.patch("music_annotator._net.time.sleep")
        assert fetch_acoustid_id("rec-mbid", no_cache=True) == "acoustid-uuid-123"

    def test_non_dict_response_returns_empty(self, mocker: MockerFixture) -> None:
        """A non-dict JSON response (e.g. a list) returns an empty string.

        :param mocker: pytest-mock fixture.
        """
        self._make_resp(mocker, b'["unexpected"]')
        mocker.patch("music_annotator._net.time.sleep")
        assert fetch_acoustid_id("rec-mbid", no_cache=True) == ""

    def test_missing_tracks_key_returns_empty(self, mocker: MockerFixture) -> None:
        """A response dict with no 'tracks' key returns an empty string.

        :param mocker: pytest-mock fixture.
        """
        self._make_resp(mocker, b'{"status": "ok"}')
        mocker.patch("music_annotator._net.time.sleep")
        assert fetch_acoustid_id("rec-mbid", no_cache=True) == ""

    def test_empty_tracks_list_returns_empty(self, mocker: MockerFixture) -> None:
        """A response with an empty 'tracks' list returns an empty string.

        :param mocker: pytest-mock fixture.
        """
        self._make_resp(mocker, b'{"tracks": []}')
        mocker.patch("music_annotator._net.time.sleep")
        assert fetch_acoustid_id("rec-mbid", no_cache=True) == ""

    def test_non_dict_first_track_returns_empty(self, mocker: MockerFixture) -> None:
        """A response where the first track element is not a dict returns an empty string.

        :param mocker: pytest-mock fixture.
        """
        self._make_resp(mocker, b'{"tracks": ["not-a-dict"]}')
        mocker.patch("music_annotator._net.time.sleep")
        assert fetch_acoustid_id("rec-mbid", no_cache=True) == ""

    def test_empty_track_id_returns_empty(self, mocker: MockerFixture) -> None:
        """A response where the first track has an empty 'id' value returns an empty string.

        :param mocker: pytest-mock fixture.
        """
        self._make_resp(mocker, b'{"tracks": [{"id": ""}]}')
        mocker.patch("music_annotator._net.time.sleep")
        assert fetch_acoustid_id("rec-mbid", no_cache=True) == ""

    def test_network_error_exhausted_raises(self, mocker: MockerFixture) -> None:
        """All three retry attempts fail with OSError; raises (cannot-determine → fatal).

        Per the universal terminal rule, RETRY exhaustion raises rather than returning empty.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.urllib.request.urlopen", side_effect=OSError("network failure"))
        mocker.patch("music_annotator._net.time.sleep")
        with pytest.raises(OSError):
            fetch_acoustid_id("rec-mbid", no_cache=True)

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
        mocker.patch("music_annotator._net.time.sleep")
        assert fetch_acoustid_id("rec-mbid", no_cache=True) == "acoustid-uuid-456"

    def test_json_decode_error_raises(self, mocker: MockerFixture) -> None:
        """A JSONDecodeError raises immediately (cannot-determine → fatal, per universal terminal rule).

        :param mocker: pytest-mock fixture.
        """
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.read = MagicMock(return_value=b"not valid json {{{")
        mock_urlopen = mocker.patch("music_annotator._mb_api.urllib.request.urlopen", return_value=ctx)
        mocker.patch("music_annotator._net.time.sleep")
        with pytest.raises(json.JSONDecodeError):
            fetch_acoustid_id("rec-mbid", no_cache=True)
        assert mock_urlopen.call_count == 1  # not retried

    def test_success_sleeps_one_second(self, mocker: MockerFixture) -> None:
        """A successful response is followed by a 1-second polite delay.

        :param mocker: pytest-mock fixture.
        """
        self._make_resp(mocker, b'{"tracks": [{"id": "acoustid-uuid-789"}]}')
        mock_sleep = mocker.patch("music_annotator._net.time.sleep")
        fetch_acoustid_id("rec-mbid", no_cache=True)
        mock_sleep.assert_called_once_with(1.0)


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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            return _rec(
                {"id": rec_id, "title": "Track", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []}
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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
# run() — disc_override
# ---------------------------------------------------------------------------


class TestRunDiscOverride:
    """Tests for run() disc_override parameter."""

    def _patch_mb_multi(self, mocker: MockerFixture, release: MBRelease) -> None:
        """Patch all MB API calls for a multi-disc run.

        :param mocker: pytest-mock fixture.
        :param release: Release model to return from fetch_release.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            return _rec(
                {"id": rec_id, "title": "Track", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []}
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

    def test_override_selects_correct_disc(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """disc_override=2 on a 3-disc release selects disc 2 directly.

        Three-disc release (2 + 3 + 2 tracks); source dir has 3 files.  Without override the
        unique-track-count heuristic would be ambiguous (discs 1 and 3 both have 2 tracks, disc 2
        has 3).  With disc_override=2 disc 2 (3 tracks) is selected and the run succeeds.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 4):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)

        release = _make_multi_disc_release([2, 3, 2])
        self._patch_mb_multi(mocker, release)

        music_annotator.run(
            release_id="rel-multi",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            disc_override=2,
        )
        flac_files = list(dest.rglob("*.flac"))
        assert len(flac_files) == 3

    def test_override_bypasses_heuristics(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """disc_override skips _select_medium_with_reason entirely.

        Two-disc release (2 + 2 tracks); without override the fallback heuristic would pick
        disc 1.  With disc_override=2, disc 2 is used without calling the selector.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 3):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)

        release = _make_multi_disc_release([2, 2])
        self._patch_mb_multi(mocker, release)
        mock_selector = mocker.patch("music_annotator._pipeline._select_medium_with_reason")

        music_annotator.run(
            release_id="rel-multi",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
            disc_override=2,
        )
        mock_selector.assert_not_called()
        # Disc 2 tracks have ids "trk-d2-1" and "trk-d2-2"; verify the destination files exist.
        flac_files = list(dest.rglob("*.flac"))
        assert len(flac_files) == 2

    def test_override_unknown_position_raises_value_error(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """disc_override pointing to a non-existent position raises ValueError with an informative message.

        Two-disc release (2 + 3 tracks); disc_override=5 → ValueError.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_multi_disc_release([2, 3])
        self._patch_mb_multi(mocker, release)

        with pytest.raises(ValueError, match="--disc 5 not found"):
            music_annotator.run(
                release_id="rel-multi",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=False,
                fetch_rels=False,
                disc_override=5,
            )

    def test_override_on_single_medium_release_correct_position(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """disc_override on a single-medium release at the correct position succeeds normally.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_multi_disc_release([1])
        self._patch_mb_multi(mocker, release)

        music_annotator.run(
            release_id="rel-multi",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
            disc_override=1,
        )
        flac_files = list(dest.rglob("*.flac"))
        assert len(flac_files) == 1

    def test_override_on_single_medium_wrong_position_raises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """disc_override with a wrong position on a single-medium release raises ValueError.

        Single-medium release at position 1; disc_override=2 → ValueError.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_multi_disc_release([1])
        self._patch_mb_multi(mocker, release)

        with pytest.raises(ValueError, match="--disc 2 not found"):
            music_annotator.run(
                release_id="rel-multi",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=False,
                fetch_rels=False,
                disc_override=2,
            )

    def test_no_override_behaviour_unchanged(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Without disc_override the existing heuristic path runs as before.

        Two-disc release (3 + 2 tracks); source dir has 2 files → disc 2 auto-selected.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 3):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)

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
        flac_files = list(dest.rglob("*.flac"))
        assert len(flac_files) == 2


# ---------------------------------------------------------------------------
# _match_medium_by_toc
# ---------------------------------------------------------------------------

#: CD frame offsets for a fictional disc 1 (4 tracks).
_DISC1_OFFSETS_MULTI: list[int] = [182, 50000, 100000, 150000]
#: CD frame offsets for a fictional disc 2 (4 tracks).
_DISC2_OFFSETS_MULTI: list[int] = [182, 60000, 110000, 160000]


def _medium_with_toc_multi(position: int, offsets: list[int]) -> MBMedium:
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
        m1 = _medium_with_toc_multi(1, _DISC1_OFFSETS_MULTI)
        m2 = _medium_with_toc_multi(2, _DISC2_OFFSETS_MULTI)
        result = _match_medium_by_toc([m1, m2], _DISC2_OFFSETS_MULTI)
        assert result is m2

    def test_matches_disc1_offsets(self) -> None:
        """Returns disc 1 when disc 1 offsets are supplied."""
        m1 = _medium_with_toc_multi(1, _DISC1_OFFSETS_MULTI)
        m2 = _medium_with_toc_multi(2, _DISC2_OFFSETS_MULTI)
        result = _match_medium_by_toc([m1, m2], _DISC1_OFFSETS_MULTI)
        assert result is m1

    def test_no_match_returns_none(self) -> None:
        """Returns None when no medium's disc offsets match the supplied track_frames."""
        m1 = _medium_with_toc_multi(1, _DISC1_OFFSETS_MULTI)
        m2 = _medium_with_toc_multi(2, _DISC2_OFFSETS_MULTI)
        result = _match_medium_by_toc([m1, m2], [182, 99999, 199999, 299999])
        assert result is None

    def test_empty_disc_list_returns_none(self) -> None:
        """Returns None when mediums have no disc entries (discids not fetched)."""
        m1 = MBMedium.model_validate({"position": 1, "format": "CD", "track-list": []})
        m2 = MBMedium.model_validate({"position": 2, "format": "CD", "track-list": []})
        result = _match_medium_by_toc([m1, m2], _DISC1_OFFSETS_MULTI)
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
                    {"offset-list": _DISC1_OFFSETS_MULTI, "sectors": str(_DISC1_OFFSETS_MULTI[-1] + 1000)},
                ],
            }
        )
        result = _match_medium_by_toc([medium], _DISC1_OFFSETS_MULTI)
        assert result is medium

    def test_fuzzy_match_plus_one_per_track(self) -> None:
        """Matches when every YAML offset is exactly 1 frame less than the MB offset.

        This is the real-world case seen with dBpowerAMP vs MusicBrainz counting conventions.
        """
        mb_offsets = [183, 114258]
        yaml_offsets = [182, 114257]  # each off by -1
        medium = _medium_with_toc_multi(2, mb_offsets)
        result = _match_medium_by_toc([medium], yaml_offsets)
        assert result is medium

    def test_fuzzy_match_minus_one_per_track(self) -> None:
        """Matches when every YAML offset is exactly 1 frame more than the MB offset."""
        mb_offsets = [182, 114257]
        yaml_offsets = [183, 114258]  # each off by +1
        medium = _medium_with_toc_multi(2, mb_offsets)
        result = _match_medium_by_toc([medium], yaml_offsets)
        assert result is medium

    def test_fuzzy_match_logs_warning(self, mocker: MockerFixture) -> None:
        """A fuzzy TOC match (±1 frame) emits a toc_match_fuzzy warning log.

        :param mocker: pytest-mock fixture.
        """
        mb_offsets = [183, 114258]
        yaml_offsets = [182, 114257]
        medium = _medium_with_toc_multi(2, mb_offsets)
        mock_warn = mocker.patch("music_annotator._pipeline.log.warning")
        _match_medium_by_toc([medium], yaml_offsets)
        mock_warn.assert_called_once()
        call_args = mock_warn.call_args
        assert call_args.args[0] == "toc_match_fuzzy"

    def test_exact_match_does_not_log_warning(self, mocker: MockerFixture) -> None:
        """An exact TOC match does not emit a warning log.

        :param mocker: pytest-mock fixture.
        """
        medium = _medium_with_toc_multi(1, _DISC1_OFFSETS_MULTI)
        mock_warn = mocker.patch("music_annotator._pipeline.log.warning")
        _match_medium_by_toc([medium], _DISC1_OFFSETS_MULTI)
        mock_warn.assert_not_called()

    def test_offset_diff_of_two_does_not_match(self) -> None:
        """Offsets differing by 2 frames are outside tolerance and do not match."""
        mb_offsets = [183, 114258]
        yaml_offsets = [181, 114256]  # each off by -2
        medium = _medium_with_toc_multi(2, mb_offsets)
        result = _match_medium_by_toc([medium], yaml_offsets)
        assert result is None

    def test_different_length_offsets_do_not_match(self) -> None:
        """Lists of different lengths never match, even if individual values are close."""
        mb_offsets = [183, 114258, 200000]
        yaml_offsets = [183, 114258]
        medium = _medium_with_toc_multi(2, mb_offsets)
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
        m1 = _medium_with_toc_multi(1, _DISC1_OFFSETS_MULTI)
        m2 = _medium_with_toc_multi(2, _DISC2_OFFSETS_MULTI)
        result, method = _select_medium_with_reason([m1, m2], 4, "dir", track_frames=_DISC2_OFFSETS_MULTI)
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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            return _rec(
                {"id": rec_id, "title": "Track", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []}
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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
# run() — track-count mismatch operator override (C-OVR)
# ---------------------------------------------------------------------------


class TestRunCountMismatch:
    """Tests for the track-count mismatch gate and operator override (C-OVR).

    KATs: test_count_mismatch_accept_ingests_partial, test_count_mismatch_decline_skips,
    test_count_mismatch_dry_run_still_raises, test_count_mismatch_no_ui_still_raises,
    test_multidisc_no_match_reaches_override, test_multidisc_no_match_dry_run_still_raises.
    """

    def _patch_mb_single(self, mocker: MockerFixture, n_medium: int) -> None:
        """Patch MB API for a single-medium release with ``n_medium`` tracks.

        :param mocker: pytest-mock fixture.
        :param n_medium: Number of tracks on the single medium.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_release(n_medium))
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_make_rec_detail)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

    def _patch_mb_multi_no_match(self, mocker: MockerFixture) -> None:
        """Patch MB API for a 2-medium release where neither medium matches 3 source files.

        Medium 1 has 4 tracks; medium 2 has 5 tracks.  Source has 3 files — no match.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_multi_disc_release([4, 5]))
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_make_rec_detail)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

    def test_count_mismatch_accept_ingests_partial_src_fewer(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Accepted mismatch with n_src < n_medium ingests k=n_src tracks at mb-partial.

        KAT: test_count_mismatch_accept_ingests_partial (n_src < n_medium direction).
        Exercises the copy-plan build IndexError guard: src_files is shorter than copy_subset.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        # 2 source files, but medium has 3 tracks → n_src < n_medium
        for i in range(1, 3):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        self._patch_mb_single(mocker, n_medium=3)

        mock_ui = MagicMock()
        mock_ui.confirm_count_mismatch.return_value = True

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
            ui=mock_ui,
        )

        mock_ui.confirm_count_mismatch.assert_called_once()
        # Verify annotation_tier is mb-partial in the sidecar
        flac_files = sorted(dest.rglob("*.flac"))
        assert len(flac_files) == 2, f"expected 2 FLAC files (k=min(2,3)=2), got {len(flac_files)}"
        work_top = _work_top_dir(flac_files[0], dest)
        prov_path = _find_freedb_sidecar(work_top) or (work_top / PROVENANCE_FILENAME)
        sidecar = _read_provenance_sidecar(prov_path)
        assert sidecar.annotation_tier == AnnotationTier.MB_PARTIAL

    def test_count_mismatch_accept_ingests_partial_src_more(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Accepted mismatch with n_src > n_medium ingests k=n_medium tracks at mb-partial.

        KAT: test_count_mismatch_accept_ingests_partial (n_src > n_medium direction).
        Exercises the ISRC tier-probe IndexError guard: src_files is longer than track_list.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        # 4 source files, but medium has 3 tracks → n_src > n_medium
        for i in range(1, 5):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        self._patch_mb_single(mocker, n_medium=3)

        mock_ui = MagicMock()
        mock_ui.confirm_count_mismatch.return_value = True

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
            ui=mock_ui,
        )

        mock_ui.confirm_count_mismatch.assert_called_once()
        flac_files = sorted(dest.rglob("*.flac"))
        assert len(flac_files) == 3, f"expected 3 FLAC files (k=min(4,3)=3), got {len(flac_files)}"
        work_top = _work_top_dir(flac_files[0], dest)
        prov_path = _find_freedb_sidecar(work_top) or (work_top / PROVENANCE_FILENAME)
        sidecar = _read_provenance_sidecar(prov_path)
        assert sidecar.annotation_tier == AnnotationTier.MB_PARTIAL

    def test_count_mismatch_decline_skips(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When the operator declines the mismatch override, RuntimeError is raised.

        KAT: test_count_mismatch_decline_skips.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 3):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        self._patch_mb_single(mocker, n_medium=3)

        mock_ui = MagicMock()
        mock_ui.confirm_count_mismatch.return_value = False

        with pytest.raises(RuntimeError, match="track count mismatch"):
            music_annotator.run(
                release_id="rel-1",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=False,
                fetch_rels=False,
                ui=mock_ui,
            )

        mock_ui.confirm_count_mismatch.assert_called_once()

    def test_count_mismatch_dry_run_still_raises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """In dry-run mode the mismatch gate raises RuntimeError without prompting.

        KAT: test_count_mismatch_dry_run_still_raises.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 3):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        self._patch_mb_single(mocker, n_medium=3)

        mock_ui = MagicMock()

        with pytest.raises(RuntimeError, match="track count mismatch"):
            music_annotator.run(
                release_id="rel-1",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=True,
                fetch_rels=False,
                ui=mock_ui,
            )

        mock_ui.confirm_count_mismatch.assert_not_called()

    def test_count_mismatch_no_ui_still_raises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When ui=None the mismatch gate raises RuntimeError without prompting.

        KAT: test_count_mismatch_no_ui_still_raises.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 3):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        self._patch_mb_single(mocker, n_medium=3)

        with pytest.raises(RuntimeError, match="track count mismatch"):
            music_annotator.run(
                release_id="rel-1",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=False,
                fetch_rels=False,
                ui=None,
            )

    def test_multidisc_no_match_reaches_override(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Multi-disc no-match path reaches confirm_count_mismatch and ingests at mb-partial on accept.

        KAT: test_multidisc_no_match_reaches_override.
        Source has 3 files; medium 1 has 4 tracks, medium 2 has 5 tracks — no exact match.
        Best medium is disc 1 (nearest count: |4-3|=1 < |5-3|=2).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 4):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        self._patch_mb_multi_no_match(mocker)

        mock_ui = MagicMock()
        mock_ui.confirm_count_mismatch.return_value = True

        music_annotator.run(
            release_id="rel-multi",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
            ui=mock_ui,
        )

        mock_ui.confirm_count_mismatch.assert_called_once()
        # Best medium is disc 1 (4 tracks, nearest to 3); k = min(3, 4) = 3
        call_args = mock_ui.confirm_count_mismatch.call_args
        assert call_args.args[3] == 3  # n_src
        assert call_args.args[4] == 4  # n_medium (disc 1)
        flac_files = sorted(dest.rglob("*.flac"))
        assert len(flac_files) == 3, f"expected 3 FLAC files (k=min(3,4)=3), got {len(flac_files)}"
        work_top = _work_top_dir(flac_files[0], dest)
        prov_path = _find_freedb_sidecar(work_top) or (work_top / PROVENANCE_FILENAME)
        sidecar = _read_provenance_sidecar(prov_path)
        assert sidecar.annotation_tier == AnnotationTier.MB_PARTIAL

    def test_multidisc_no_match_dry_run_still_raises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """In dry-run mode the multi-disc no-match path re-raises ValueError without prompting.

        KAT: test_multidisc_no_match_dry_run_still_raises.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 4):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        self._patch_mb_multi_no_match(mocker)

        mock_ui = MagicMock()

        with pytest.raises(ValueError, match="track count mismatch"):
            music_annotator.run(
                release_id="rel-multi",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=True,
                fetch_rels=False,
                ui=mock_ui,
            )

        mock_ui.confirm_count_mismatch.assert_not_called()

    def test_multidisc_no_match_no_ui_still_raises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When ui=None the multi-disc no-match path re-raises ValueError without prompting.

        Covers the no-ui branch of the no-match ValueError re-raise.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 4):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        self._patch_mb_multi_no_match(mocker)

        with pytest.raises(ValueError, match="track count mismatch"):
            music_annotator.run(
                release_id="rel-multi",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=False,
                fetch_rels=False,
                ui=None,
            )

    def test_multidisc_no_match_decline_reraises(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When the operator declines the multi-disc no-match override, ValueError is re-raised.

        Covers the decline branch (line 1567) of the no-match ValueError path.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 4):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        self._patch_mb_multi_no_match(mocker)

        mock_ui = MagicMock()
        mock_ui.confirm_count_mismatch.return_value = False

        with pytest.raises(ValueError, match="track count mismatch"):
            music_annotator.run(
                release_id="rel-multi",
                src_dir=src,
                dest_root=dest,
                user_agent="Test/1.0",
                dry_run=False,
                fetch_rels=False,
                ui=mock_ui,
            )

        mock_ui.confirm_count_mismatch.assert_called_once()

    def test_multidisc_selected_medium_count_mismatch_diagnostic(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Multi-disc release with disc_override: selected medium count mismatch uses multi-disc diagnostic.

        Covers the ``n_disc > 1`` branch of the diagnostic string in the single-medium mismatch gate.
        disc_override selects disc 1 (4 tracks) but source has 3 files → mismatch gate fires with
        the multi-disc diagnostic string.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        # 3 source files; disc_override=1 selects disc 1 (4 tracks) → mismatch
        for i in range(1, 4):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        # 2-medium release: disc 1 has 4 tracks, disc 2 has 3 tracks
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=_make_multi_disc_release([4, 3]))
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_make_rec_detail)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

        mock_ui = MagicMock()
        mock_ui.confirm_count_mismatch.return_value = True

        music_annotator.run(
            release_id="rel-multi",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
            ui=mock_ui,
            disc_override=1,  # force disc 1 (4 tracks) even though source has 3 files
        )

        mock_ui.confirm_count_mismatch.assert_called_once()
        # Verify the diagnostic string mentions the multi-disc context
        call_args = mock_ui.confirm_count_mismatch.call_args
        diagnostic: str = call_args.args[5]
        assert "disc 1 of 2" in diagnostic


# ---------------------------------------------------------------------------
# compare_audio_collision / _assess_collisions / _collision_suffix /
# _apply_collision_suffix
# ---------------------------------------------------------------------------


class TestCompareAudioCollision:
    """Tests for compare_audio_collision — the layered audio comparison function."""

    def test_sha256_match_returns_match_true(self, fs: FakeFilesystem) -> None:
        """Byte-identical src and dest produce match=True, method='sha256'.

        :param fs: pyfakefs fixture.
        """
        data = b"audio bytes"
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=data)
        fs.create_file(str(dest), contents=data)

        result = compare_audio_collision(src, dest, "", 0)
        assert result.match is True
        assert result.method == "sha256"

    def test_acoustid_match_returns_match_true(self, fs: FakeFilesystem) -> None:
        """Matching AcoustID UUIDs produce match=True, method='acoustid'.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        fs.create_file(str(dest), contents=_MINIMAL_FLAC[:16] + b"\xff" * 4 + _MINIMAL_FLAC[20:])
        apply_tags_flac(dest, TrackTags(title="X", acoustid_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))

        result = compare_audio_collision(src, dest, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", 0)
        assert result.match is True
        assert result.method == "acoustid"

    def test_acoustid_mismatch_returns_match_false(self, fs: FakeFilesystem) -> None:
        """Differing AcoustID UUIDs produce match=False, method='acoustid'.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        dest_bytes = _MINIMAL_FLAC[:16] + b"\xff" * 4 + _MINIMAL_FLAC[20:]
        fs.create_file(str(dest), contents=dest_bytes)
        apply_tags_flac(dest, TrackTags(title="X", acoustid_id="11111111-2222-3333-4444-555555555555"))

        result = compare_audio_collision(src, dest, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", 0)
        assert result.match is False
        assert result.method == "acoustid"

    def test_duration_outside_tolerance_returns_match_false(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Duration difference > 2000 ms produces match=False, method='duration'.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        dest_bytes = _MINIMAL_FLAC[:16] + b"\xff" * 4 + _MINIMAL_FLAC[20:]
        fs.create_file(str(dest), contents=dest_bytes)
        mocker.patch("music_annotator._pipeline_io.shutil.which", return_value=None)
        mocker.patch("music_annotator._pipeline_io._read_duration_ms", return_value=60_000)

        # incoming 45_000 ms, dest 60_000 ms → delta 15_000 ms >> 2000 ms tolerance
        result = compare_audio_collision(src, dest, "", 45_000)
        assert result.match is False
        assert result.method == "duration"

    def test_duration_within_tolerance_no_fpcalc_returns_inconclusive(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Duration within ±2000 ms with no fpcalc produces match=None, method='duration'.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        dest_bytes = _MINIMAL_FLAC[:16] + b"\xff" * 4 + _MINIMAL_FLAC[20:]
        fs.create_file(str(dest), contents=dest_bytes)
        mocker.patch("music_annotator._pipeline_io.shutil.which", return_value=None)
        mocker.patch("music_annotator._pipeline_io._read_duration_ms", return_value=60_500)

        result = compare_audio_collision(src, dest, "", 60_000)
        assert result.match is None
        assert result.method == "duration"

    def test_fpcalc_match_returns_match_true(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Identical fpcalc fingerprints produce match=True, method='chromaprint'.

        Uses a valid base64url-encoded fingerprint (4 × 32-bit integers) so the fuzzy
        Hamming-distance comparison can decode it.  Identical fingerprints yield similarity=1.0
        which is ≥ the 0.90 threshold.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        # 4 × 32-bit integers → 16 bytes → valid Chromaprint fingerprint payload
        fp_bytes = struct.pack("<4I", 0x12345678, 0xABCDEF01, 0x87654321, 0x10FEDCBA)
        fp_str = base64.b64encode(fp_bytes).decode().rstrip("=").replace("+", "-").replace("/", "_")

        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        dest_bytes = _MINIMAL_FLAC[:16] + b"\xff" * 4 + _MINIMAL_FLAC[20:]
        fs.create_file(str(dest), contents=dest_bytes)
        mocker.patch("music_annotator._pipeline_io.shutil.which", return_value="/usr/bin/fpcalc")
        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value=fp_str)

        result = compare_audio_collision(src, dest, "", 0)
        assert result.match is True
        assert result.method == "chromaprint"
        assert "similarity=1.000" in result.detail

    def test_fpcalc_mismatch_returns_match_false(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Sufficiently different fpcalc fingerprints produce match=False, method='chromaprint'.

        Uses valid base64url-encoded fingerprints where all bits are flipped between src and dest,
        yielding similarity=0.0 which is below the 0.90 threshold.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        # src fingerprint: all zeros; dest fingerprint: all ones → all bits differ → similarity=0.0
        fp_src_bytes = struct.pack("<4I", 0x00000000, 0x00000000, 0x00000000, 0x00000000)
        fp_dest_bytes = struct.pack("<4I", 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
        fp_src = base64.b64encode(fp_src_bytes).decode().rstrip("=").replace("+", "-").replace("/", "_")
        fp_dest = base64.b64encode(fp_dest_bytes).decode().rstrip("=").replace("+", "-").replace("/", "_")

        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        dest_bytes = _MINIMAL_FLAC[:16] + b"\xff" * 4 + _MINIMAL_FLAC[20:]
        fs.create_file(str(dest), contents=dest_bytes)
        mocker.patch("music_annotator._pipeline_io.shutil.which", return_value="/usr/bin/fpcalc")
        fingerprints = iter([fp_src, fp_dest])
        mocker.patch("music_annotator._pipeline_io._run_fpcalc", side_effect=lambda _p: next(fingerprints))

        result = compare_audio_collision(src, dest, "", 0)
        assert result.match is False
        assert result.method == "chromaprint"
        assert "similarity=0.000" in result.detail

    def test_no_acoustid_no_fpcalc_no_duration_returns_unknown(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """With no AcoustID, no fpcalc, and no length data the result is match=None, method='unknown'.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        dest_bytes = _MINIMAL_FLAC[:16] + b"\xff" * 4 + _MINIMAL_FLAC[20:]
        fs.create_file(str(dest), contents=dest_bytes)
        mocker.patch("music_annotator._pipeline_io.shutil.which", return_value=None)

        result = compare_audio_collision(src, dest, "", 0)
        assert result.match is None
        assert result.method == "unknown"

    def test_fpcalc_warning_emitted_once_when_duration_candidate(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """A structlog warning is emitted when fpcalc is absent and duration flagged a candidate match.

        The warning is emitted at most once per process lifetime (module-level flag).  We reset the
        flag before calling to ensure a deterministic result.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        dest_bytes = _MINIMAL_FLAC[:16] + b"\xff" * 4 + _MINIMAL_FLAC[20:]
        fs.create_file(str(dest), contents=dest_bytes)
        mocker.patch("music_annotator._pipeline_io.shutil.which", return_value=None)
        mocker.patch("music_annotator._pipeline_io._read_duration_ms", return_value=60_500)
        mock_warn = mocker.patch("music_annotator._pipeline_io.log.warning")
        _pio._FPCALC_WARNED[0] = False  # reset module-level flag  # pylint: disable=protected-access  # noqa: SLF001

        compare_audio_collision(src, dest, "", 60_000)

        assert mock_warn.called
        assert any("fpcalc_not_found" in str(call) for call in mock_warn.call_args_list)

    def test_read_acoustid_tag_mp3_returns_value(self, fs: FakeFilesystem) -> None:
        """_read_acoustid_tag reads the ACOUSTID_ID TXXX frame from an MP3 file.

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(path), contents=_MINIMAL_MP3)
        tags = TrackTags(title="X", acoustid_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        apply_tags_mp3(path, tags)
        assert _read_acoustid_tag(path) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def test_read_acoustid_tag_mp3_no_txxx_returns_empty(self, fs: FakeFilesystem) -> None:
        """_read_acoustid_tag returns '' for an MP3 file that has no 'Acoustid Id' TXXX frame.

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(path), contents=_MINIMAL_MP3)
        # Write tags with no acoustid_id so no TXXX 'Acoustid Id' frame is present.
        apply_tags_mp3(path, TrackTags(title="X"))
        assert _read_acoustid_tag(path) == ""

    def test_read_duration_ms_info_no_length_attr_returns_zero(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_read_duration_ms returns 0 when the mutagen object's info has no 'length' attribute.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.flac")
        fs.create_dir("/out")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)

        class _FakeInfo:
            pass  # no 'length' attribute

        class _FakeAudio:
            info = _FakeInfo()

        mocker.patch("music_annotator._pipeline_io.MutagenFile", return_value=_FakeAudio())
        assert _read_duration_ms(path) == 0

    def test_duration_comparison_dest_length_zero_falls_through(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When _read_duration_ms returns 0 for dest, duration comparison is skipped.

        The condition ``if dest_length_ms > 0`` is False, so we fall through to 'unknown'.
        Covers the branch 242->268 (dest_length_ms == 0 arm).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        dest_bytes = _MINIMAL_FLAC[:16] + b"\xff" * 4 + _MINIMAL_FLAC[20:]
        fs.create_file(str(dest), contents=dest_bytes)
        mocker.patch("music_annotator._pipeline_io.shutil.which", return_value=None)
        mocker.patch("music_annotator._pipeline_io._read_duration_ms", return_value=0)

        # incoming_length_ms=60_000 so we enter the duration block, but dest returns 0
        result = compare_audio_collision(src, dest, "", 60_000)
        assert result.match is None
        assert result.method == "unknown"

    def test_read_acoustid_tag_unsupported_extension_returns_empty(self, fs: FakeFilesystem) -> None:
        """_read_acoustid_tag returns '' for unsupported file extensions.

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.wav")
        fs.create_file(str(path), contents=b"RIFF")
        assert _read_acoustid_tag(path) == ""

    def test_read_acoustid_tag_exception_returns_empty(self, fs: FakeFilesystem) -> None:
        """_read_acoustid_tag returns '' when mutagen raises an exception.

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.flac")
        fs.create_file(str(path), contents=b"not a real flac")
        # Corrupt content causes mutagen to raise; the function must swallow it.
        assert _read_acoustid_tag(path) == ""

    def test_read_duration_ms_returns_milliseconds(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_read_duration_ms returns duration in milliseconds when mutagen reads info.length.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.flac")
        fs.create_dir("/out")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)

        class _FakeInfo:
            length: float = 60.5

        class _FakeAudio:
            info = _FakeInfo()

        mocker.patch("music_annotator._pipeline_io.MutagenFile", return_value=_FakeAudio())
        assert _read_duration_ms(path) == 60_500

    def test_read_duration_ms_no_info_returns_zero(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_read_duration_ms returns 0 when mutagen returns a file object with no info attribute.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.flac")
        fs.create_dir("/out")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)

        class _FakeAudioNoInfo:
            pass  # no 'info' attribute

        mocker.patch("music_annotator._pipeline_io.MutagenFile", return_value=_FakeAudioNoInfo())
        assert _read_duration_ms(path) == 0

    def test_read_duration_ms_exception_returns_zero(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_read_duration_ms returns 0 when mutagen raises an exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.flac")
        fs.create_file(str(path), contents=b"not a real flac")
        mocker.patch("music_annotator._pipeline_io.MutagenFile", side_effect=OSError("read error"))
        assert _read_duration_ms(path) == 0

    def test_run_fpcalc_nonzero_returncode_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_run_fpcalc returns '' when fpcalc exits with a non-zero return code.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.flac")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)
        mocker.patch(
            "music_annotator._pipeline_io.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=""),
        )
        assert _run_fpcalc(path) == ""

    def test_run_fpcalc_non_dict_json_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_run_fpcalc returns '' when fpcalc outputs valid JSON that is not a dict.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.flac")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)
        mocker.patch(
            "music_annotator._pipeline_io.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="[1, 2, 3]", stderr=""),
        )
        assert _run_fpcalc(path) == ""

    def test_run_fpcalc_json_decode_error_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_run_fpcalc returns '' when fpcalc output cannot be parsed as JSON.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.flac")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)
        mocker.patch(
            "music_annotator._pipeline_io.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr=""),
        )
        assert _run_fpcalc(path) == ""

    def test_run_fpcalc_fingerprint_key_missing_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_run_fpcalc returns '' when fpcalc JSON has no 'fingerprint' key.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.flac")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)
        mocker.patch(
            "music_annotator._pipeline_io.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout='{"duration": 60.0}', stderr=""),
        )
        assert _run_fpcalc(path) == ""

    def test_fpcalc_present_but_empty_fingerprints_falls_through_to_unknown(
        self, mocker: MockerFixture, fs: FakeFilesystem
    ) -> None:
        """When fpcalc is present but returns empty fingerprints, the result falls through to 'unknown'.

        This exercises the branch where fpcalc_path is set but _run_fpcalc returns ''.
        No AcoustID tags, no length → match=None, method='unknown'.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        dest_bytes = _MINIMAL_FLAC[:16] + b"\xff" * 4 + _MINIMAL_FLAC[20:]
        fs.create_file(str(dest), contents=dest_bytes)
        mocker.patch("music_annotator._pipeline_io.shutil.which", return_value="/usr/bin/fpcalc")
        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="")

        result = compare_audio_collision(src, dest, "", 0)
        assert result.match is None
        assert result.method == "unknown"

    def test_fpcalc_present_duration_within_tolerance_falls_through_to_unknown(
        self, mocker: MockerFixture, fs: FakeFilesystem
    ) -> None:
        """When fpcalc is present but returns no fingerprint and duration is within tolerance.

        The result should be match=None, method='unknown' because fpcalc is present (line 259
        guarded by ``if fpcalc_path is None`` is skipped), so we don't return early from
        duration, and fall through to the final 'unknown' result.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        dest_bytes = _MINIMAL_FLAC[:16] + b"\xff" * 4 + _MINIMAL_FLAC[20:]
        fs.create_file(str(dest), contents=dest_bytes)
        mocker.patch("music_annotator._pipeline_io.shutil.which", return_value="/usr/bin/fpcalc")
        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="")
        mocker.patch("music_annotator._pipeline_io._read_duration_ms", return_value=60_500)

        result = compare_audio_collision(src, dest, "", 60_000)
        assert result.match is None
        assert result.method == "unknown"


# ---------------------------------------------------------------------------
# _chromaprint_similarity — unit tests for the Hamming-distance helper
# ---------------------------------------------------------------------------


class TestChromaprintSimilarity:
    """Unit tests for _chromaprint_similarity — the Hamming-distance fingerprint comparison helper."""

    def _make_fp(self, ints: list[int]) -> str:
        """Encode a list of 32-bit unsigned integers as a base64url fingerprint string.

        :param ints: List of unsigned 32-bit integers to encode.
        :returns: Base64url-encoded fingerprint string (no padding).
        """
        data = struct.pack(f"<{len(ints)}I", *ints)
        return base64.b64encode(data).decode().rstrip("=").replace("+", "-").replace("/", "_")

    def test_identical_fingerprints_return_similarity_one(self) -> None:
        """Two identical fingerprints yield similarity=1.0."""
        fp = self._make_fp([0x12345678, 0xABCDEF01, 0x87654321, 0x10FEDCBA])
        result = _chromaprint_similarity(fp, fp)
        assert result is not None
        assert abs(result - 1.0) < 1e-9

    def test_all_bits_flipped_returns_similarity_zero(self) -> None:
        """Fingerprints with all bits flipped yield similarity=0.0."""
        fp_a = self._make_fp([0x00000000, 0x00000000, 0x00000000, 0x00000000])
        fp_b = self._make_fp([0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF])
        result = _chromaprint_similarity(fp_a, fp_b)
        assert result is not None
        assert abs(result - 0.0) < 1e-9

    def test_empty_first_fingerprint_returns_none(self) -> None:
        """An empty first fingerprint returns None (cannot compare)."""
        fp = self._make_fp([0x12345678, 0xABCDEF01])
        assert _chromaprint_similarity("", fp) is None

    def test_empty_second_fingerprint_returns_none(self) -> None:
        """An empty second fingerprint returns None (cannot compare)."""
        fp = self._make_fp([0x12345678, 0xABCDEF01])
        assert _chromaprint_similarity(fp, "") is None

    def test_both_empty_returns_none(self) -> None:
        """Both empty fingerprints return None."""
        assert _chromaprint_similarity("", "") is None

    def test_invalid_base64_returns_none(self) -> None:
        """A fingerprint that cannot be base64-decoded returns None."""
        assert _chromaprint_similarity("!!!invalid!!!", "!!!invalid!!!") is None

    def test_different_lengths_returns_none(self) -> None:
        """Fingerprints of different lengths (different number of integers) return None."""
        fp_short = self._make_fp([0x12345678, 0xABCDEF01])
        fp_long = self._make_fp([0x12345678, 0xABCDEF01, 0x87654321, 0x10FEDCBA])
        assert _chromaprint_similarity(fp_short, fp_long) is None

    def test_partial_bit_flip_returns_intermediate_similarity(self) -> None:
        """Flipping half the bits in one integer yields a similarity between 0 and 1."""
        # 0x0000FFFF has 16 bits set; XOR with 0x00000000 gives 16 set bits out of 32.
        fp_a = self._make_fp([0x00000000])
        fp_b = self._make_fp([0x0000FFFF])
        result = _chromaprint_similarity(fp_a, fp_b)
        assert result is not None
        # 16 bits differ out of 32 → similarity = 1 - 16/32 = 0.5
        assert abs(result - 0.5) < 1e-9

    def test_similarity_threshold_constant_is_0_90(self) -> None:
        """The module-level threshold constant equals 0.90."""
        assert _CHROMAPRINT_SIMILARITY_THRESHOLD == 0.90  # noqa: PLR2004


# ---------------------------------------------------------------------------
# KAT: test_chromaprint_fuzzy_same_recording_different_encode
# ---------------------------------------------------------------------------


class TestChromaprintFuzzyComparison:
    """KAT F3: fuzzy Hamming-distance Chromaprint comparison replaces exact equality."""

    def _make_fp(self, ints: list[int]) -> str:
        """Encode a list of 32-bit unsigned integers as a base64url fingerprint string.

        :param ints: List of unsigned 32-bit integers to encode.
        :returns: Base64url-encoded fingerprint string (no padding).
        """
        data = struct.pack(f"<{len(ints)}I", *ints)
        return base64.b64encode(data).decode().rstrip("=").replace("+", "-").replace("/", "_")

    def test_chromaprint_fuzzy_same_recording_different_encode(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Two slightly different fingerprints (similarity ≥ 0.90) match under fuzzy comparison.

        This KAT verifies that the fuzzy Hamming-distance comparison correctly identifies two
        fingerprints from the same recording that differ slightly due to different encoding
        parameters.  Exact equality would fail (the fingerprints differ), but fuzzy comparison
        succeeds because the similarity is above the 0.90 threshold.

        Construction: a 32-integer fingerprint where 2 out of 32 integers have a single bit
        flipped.  This produces 2 bit differences out of 1024 total bits, giving a similarity
        of 1 - 2/1024 ≈ 0.998, which is well above the 0.90 threshold.  Exact equality fails
        because the fingerprints are not identical.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        # Build a 32-integer fingerprint (typical Chromaprint length for a ~30s clip).
        # Use modulo to keep all values within the 32-bit unsigned integer range.
        base_ints = [(0x12345678 + i * 0x01234567) & 0xFFFFFFFF for i in range(32)]
        # Flip a single bit in two of the integers to simulate a different encode.
        # This makes the fingerprints non-identical (exact equality fails) but very similar.
        modified_ints = list(base_ints)
        modified_ints[5] ^= 0x00000001  # flip bit 0 of integer 5
        modified_ints[17] ^= 0x00010000  # flip bit 16 of integer 17

        fp_src = self._make_fp(base_ints)
        fp_dest = self._make_fp(modified_ints)

        # Verify that exact equality would fail (the fingerprints differ).
        assert fp_src != fp_dest, "Test setup error: fingerprints must differ for this KAT to be meaningful"

        # Verify that fuzzy similarity is above the threshold.
        similarity = _chromaprint_similarity(fp_src, fp_dest)
        assert similarity is not None
        assert similarity >= _CHROMAPRINT_SIMILARITY_THRESHOLD, (
            f"Expected similarity ≥ {_CHROMAPRINT_SIMILARITY_THRESHOLD}, got {similarity:.6f}"
        )

        # Now verify that compare_audio_collision returns match=True, method='chromaprint'.
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        dest_bytes = _MINIMAL_FLAC[:16] + b"\xff" * 4 + _MINIMAL_FLAC[20:]
        fs.create_file(str(dest), contents=dest_bytes)
        mocker.patch("music_annotator._pipeline_io.shutil.which", return_value="/usr/bin/fpcalc")
        fingerprints = iter([fp_src, fp_dest])
        mocker.patch("music_annotator._pipeline_io._run_fpcalc", side_effect=lambda _p: next(fingerprints))

        result = compare_audio_collision(src, dest, "", 0)
        assert result.match is True, (
            f"Expected match=True for similar fingerprints (similarity={similarity:.6f}), got match={result.match}"
        )
        assert result.method == "chromaprint"
        assert "similarity=" in result.detail

    def test_chromaprint_below_threshold_returns_match_false(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Fingerprints with similarity below 0.90 produce match=False, method='chromaprint'.

        Uses fingerprints with ~50% bit similarity (half the bits differ), which is well below
        the 0.90 threshold.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        # Alternating 0x0000FFFF and 0xFFFF0000 → XOR = 0xFFFFFFFF → all 32 bits differ per int.
        # 4 integers × 32 bits = 128 bits total; all 128 differ → similarity = 0.0.
        fp_src = self._make_fp([0x0000FFFF] * 4)
        fp_dest = self._make_fp([0xFFFF0000] * 4)

        similarity = _chromaprint_similarity(fp_src, fp_dest)
        assert similarity is not None
        assert similarity < _CHROMAPRINT_SIMILARITY_THRESHOLD

        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        dest_bytes = _MINIMAL_FLAC[:16] + b"\xff" * 4 + _MINIMAL_FLAC[20:]
        fs.create_file(str(dest), contents=dest_bytes)
        mocker.patch("music_annotator._pipeline_io.shutil.which", return_value="/usr/bin/fpcalc")
        fingerprints = iter([fp_src, fp_dest])
        mocker.patch("music_annotator._pipeline_io._run_fpcalc", side_effect=lambda _p: next(fingerprints))

        result = compare_audio_collision(src, dest, "", 0)
        assert result.match is False
        assert result.method == "chromaprint"
        assert "similarity=" in result.detail

    def test_invalid_fingerprint_falls_through_to_unknown(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When fingerprints cannot be decoded, comparison falls through to 'unknown'.

        An invalid base64 string causes _chromaprint_similarity to return None, so the
        chromaprint layer produces no result and the function falls through to the final
        'unknown' outcome (no AcoustID, no duration).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        dest_bytes = _MINIMAL_FLAC[:16] + b"\xff" * 4 + _MINIMAL_FLAC[20:]
        fs.create_file(str(dest), contents=dest_bytes)
        mocker.patch("music_annotator._pipeline_io.shutil.which", return_value="/usr/bin/fpcalc")
        # Return non-empty but invalid base64 strings so _chromaprint_similarity returns None.
        mocker.patch("music_annotator._pipeline_io._run_fpcalc", return_value="!!!invalid!!!")

        result = compare_audio_collision(src, dest, "", 0)
        assert result.match is None
        assert result.method == "unknown"


class TestAssessCollisions:
    """Tests for _assess_collisions — aggregates compare_audio_collision per plan entry."""

    def test_no_existing_dest_returns_empty(self, fs: FakeFilesystem) -> None:
        """When no destination files exist the result list is empty.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        # dest does NOT exist

        result = _assess_collisions([(src, dest, "", 0)])
        assert result == []

    def test_existing_dest_returned(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When a destination file exists its AudioCompareResult is included in the output.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/track.flac")
        dest = Path("/dest/track.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        fs.create_file(str(dest), contents=_MINIMAL_FLAC)
        mocker.patch("music_annotator._pipeline_io.shutil.which", return_value=None)

        results = _assess_collisions([(src, dest, "", 0)])
        assert len(results) == 1
        assert results[0].src == src
        assert results[0].dest == dest

    def test_mixed_existing_and_new(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Only the existing destination appears in the result, not the missing one.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src1 = Path("/src/01.flac")
        src2 = Path("/src/02.flac")
        dest1 = Path("/dest/01.flac")
        dest2 = Path("/dest/02.flac")
        for p in (src1, src2, dest1):
            fs.create_file(str(p), contents=_MINIMAL_FLAC)
        # dest2 does NOT exist
        mocker.patch("music_annotator._pipeline_io.shutil.which", return_value=None)

        results = _assess_collisions([(src1, dest1, "", 0), (src2, dest2, "", 0)])
        assert len(results) == 1
        assert results[0].dest == dest1


class TestCollisionSuffixAndApply:
    """Tests for _collision_suffix and _apply_collision_suffix."""

    def _make_nonmatch(self, dest: Path) -> AudioCompareResult:
        """Build a confirmed-nonmatch AudioCompareResult for ``dest``.

        :param dest: The destination path.
        :returns: An :class:`~music_annotator._pipeline_io.AudioCompareResult` with ``match=False``.
        """
        return AudioCompareResult(src=Path("/src/x.flac"), dest=dest, match=False, method="acoustid", detail="test")

    def test_collision_suffix_catalog_number(self) -> None:
        """_collision_suffix returns the catalog number when present.

        :raises AssertionError: If the returned suffix is not the catalog number.
        """
        release = _make_release()
        assert _collision_suffix(release) == "CAT-001"

    def test_collision_suffix_mbid_fallback(self) -> None:
        """_collision_suffix falls back to the first 8 chars of the release MBID when no catalog number.

        :raises AssertionError: If the returned suffix is not the expected MBID prefix.
        """
        release = _rel(
            {
                "id": "abcdef12-3456-7890-abcd-ef1234567890",
                "title": "Test",
                "date": "2000",
                "status": "Official",
                "barcode": "",
                "artist-credit": [],
                "release-group": {"id": "rg-1", "primary-type": "Album", "first-release-date": "2000"},
                "label-info-list": [{"label": {"id": "l1", "name": "Label"}, "catalog-number": ""}],
                "text-representation": {"script": "Latn", "language": "eng"},
                "medium-list": [],
            }
        )
        assert _collision_suffix(release) == "abcdef12"

    def test_collision_suffix_no_label_info(self) -> None:
        """_collision_suffix falls back to MBID prefix when label_info_list is empty.

        :raises AssertionError: If the returned suffix is not the MBID prefix.
        """
        release = _rel(
            {
                "id": "12345678-aaaa-bbbb-cccc-dddddddddddd",
                "title": "Test",
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
        assert _collision_suffix(release) == "12345678"

    def test_collision_suffix_empty_id_raises(self) -> None:
        """_collision_suffix raises ValueError when the release id is empty and no catalog number is present.

        An empty release id cannot yield a collision suffix: the result would be an empty string,
        producing a degenerate '[]' suffix that silently corrupts the library layout instead of
        disambiguating it.  Any caller that passes a release with an empty id has a threading
        defect; the guard raises immediately so the defect is caught rather than silently degraded.

        :raises AssertionError: If ValueError is not raised with a message naming the missing-id invariant.
        """
        with pytest.raises(ValueError, match="collision suffix cannot be derived without a release id"):
            _collision_suffix(MBRelease())

    def test_apply_collision_suffix_renames_matching_entry(self) -> None:
        """_apply_collision_suffix rewrites the work_dir component of matching plan entries.

        The plan entry whose dest_file appears in nonmatches should have its parts[1]
        (work_dir) renamed to ``<work_dir> [<suffix>]``.

        :raises AssertionError: If the destination path is not rewritten correctly.
        """
        dest_root = Path("/dest")
        work_dir = dest_root / "Brahms - Karajan" / "Symphony No. 2 [rec 1977]"
        dest = work_dir / "01 - Adagio.flac"
        plan = [CopyPlanEntry(idx=0, src_file=Path("/src/01.flac"), dest_file=dest)]
        nonmatch = self._make_nonmatch(dest)
        release = _make_release()  # catalog-number "CAT-001"

        _apply_collision_suffix(plan, [nonmatch], release, dest_root)

        new_dest = plan[0].dest_file
        assert "Symphony No. 2 [rec 1977] [CAT-001]" in str(new_dest)
        assert new_dest.name == "01 - Adagio.flac"
        assert new_dest.parent.parent.parent == dest_root

    def test_apply_collision_suffix_with_intermediate_dir(self) -> None:
        """_apply_collision_suffix correctly rewrites work_dir even with an intermediate act dir.

        :raises AssertionError: If the intermediate directory is lost or the suffix is misplaced.
        """
        dest_root = Path("/dest")
        work_dir = dest_root / "Wagner - Karajan" / "Meistersinger [rel 1999]"
        dest = work_dir / "02 - Akt I" / "03 - Scene III.flac"
        plan = [CopyPlanEntry(idx=0, src_file=Path("/src/03.flac"), dest_file=dest)]
        nonmatch = self._make_nonmatch(dest)
        release = _make_release()

        _apply_collision_suffix(plan, [nonmatch], release, dest_root)

        new_dest = plan[0].dest_file
        assert "Meistersinger [rel 1999] [CAT-001]" in str(new_dest)
        assert "02 - Akt I" in str(new_dest)
        assert new_dest.name == "03 - Scene III.flac"

    def test_apply_collision_suffix_unaffected_entry_unchanged(self) -> None:
        """Plan entries not in nonmatches are not modified.

        :raises AssertionError: If an unaffected entry is changed.
        """
        dest_root = Path("/dest")
        work_dir = dest_root / "Composer" / "Work [rel 2000]"
        dest1 = work_dir / "01.flac"
        dest2 = work_dir / "02.flac"
        plan = [
            CopyPlanEntry(idx=0, src_file=Path("/src/01.flac"), dest_file=dest1),
            CopyPlanEntry(idx=1, src_file=Path("/src/02.flac"), dest_file=dest2),
        ]
        nonmatch = self._make_nonmatch(dest1)
        release = _make_release()

        _apply_collision_suffix(plan, [nonmatch], release, dest_root)

        assert plan[1].dest_file == dest2

    def test_apply_collision_suffix_uses_mbid_fallback(self) -> None:
        """_apply_collision_suffix uses the MBID prefix when catalog number is absent.

        :raises AssertionError: If the MBID prefix is not used as the suffix.
        """
        dest_root = Path("/dest")
        work_dir = dest_root / "Composer" / "Work [rel 2000]"
        dest = work_dir / "01.flac"
        plan = [CopyPlanEntry(idx=0, src_file=Path("/src/01.flac"), dest_file=dest)]
        nonmatch = self._make_nonmatch(dest)
        release = _rel(
            {
                "id": "abcdef12-3456-7890-abcd-ef1234567890",
                "title": "Test",
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

        _apply_collision_suffix(plan, [nonmatch], release, dest_root)

        assert "Work [rel 2000] [abcdef12]" in str(plan[0].dest_file)


class TestRunCollisionAudioComparison:
    """Tests for run() behaviour when _assess_collisions returns confirmed non-matches."""

    def test_nonmatch_collision_auto_suffixes_path_no_prompt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """A confirmed non-matching collision rewrites the destination path without prompting.

        The prompt is not shown; the file is copied to the suffixed path.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        _setup_single_track_run(mocker, fs, src, dest)

        collision_path = dest / "Composer - Performer" / "Work [rec 1970]" / "existing.flac"
        fs.create_file(str(collision_path))
        nonmatch_result = AudioCompareResult(
            src=src / "01.flac",
            dest=collision_path,
            match=False,
            method="acoustid",
            detail="different AcoustID clusters",
        )
        mocker.patch("music_annotator._pipeline._assess_collisions", return_value=[nonmatch_result])
        mock_prompt = mocker.patch("music_annotator._pipeline._prompt_collision_policy")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )
        mock_prompt.assert_not_called()

    def test_match_collision_shows_prompt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """A confirmed-match collision shows the user prompt (does not auto-suffix).

        Verifies that _prompt_collision_policy is called when match=True (identical audio).
        Non-matching collisions bypass the prompt entirely; this test checks the match branch.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        _setup_single_track_run(mocker, fs, src, dest)

        collision_path = dest / "Composer - Performer" / "Work [rec 1970]" / "existing.flac"
        fs.create_file(str(collision_path))
        match_result = AudioCompareResult(
            src=src / "01.flac",
            dest=collision_path,
            match=True,
            method="sha256",
            detail="byte-identical files",
        )
        mocker.patch("music_annotator._pipeline._assess_collisions", return_value=[match_result])
        mock_prompt = mocker.patch("music_annotator._pipeline._prompt_collision_policy", return_value=CollisionPolicy.SKIP)

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )
        # Prompt must be called for confirmed-match collisions (identical audio).
        mock_prompt.assert_called_once()

    def test_invalid_length_tag_treated_as_zero(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When TrackTags.length is non-numeric, the ValueError is caught and length_ms is 0.

        Verifies the except ValueError branch in run() when building plan_pairs.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        _setup_single_track_run(mocker, fs, src, dest)

        # Patch build_track_tags to return tags with an invalid length string.
        orig_build = music_annotator._tags.build_track_tags  # pylint: disable=protected-access

        def _patched_build(
            release: MBRelease,
            track: MBTrack,
            medium_pos: int,
            recording_detail: MBRecording,
            work_hierarchy: list[MBWork],
        ) -> TrackTags:
            tags = orig_build(release, track, medium_pos, recording_detail, work_hierarchy)
            tags.length = "not-a-number"
            return tags

        mocker.patch("music_annotator._pipeline.build_track_tags", side_effect=_patched_build)
        # No collision; just verify the run completes without error.
        mocker.patch("music_annotator._pipeline._assess_collisions", return_value=[])

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,  # must be True so build_track_tags is called
        )
        assert (dest / JOURNAL_FILENAME).exists()

    def test_prompt_shows_comparison_context(self, mocker: MockerFixture) -> None:
        """The collision prompt displays the method and detail from AudioCompareResult.

        :param mocker: pytest-mock fixture.
        """
        dest = Path("/dest")
        work_dir = dest / "Brahms - Karajan" / "Symphony No. 1 [rec 1970]"
        collision_path = work_dir / "01 - Allegro.flac"
        result = AudioCompareResult(
            src=Path("/src/01.flac"),
            dest=collision_path,
            match=True,
            method="acoustid",
            detail="same AcoustID cluster (aabbccdd…)",
        )
        printed: list[str] = []
        mocker.patch("music_annotator._pipeline._console.print", side_effect=lambda s, **_: printed.append(s))
        mocker.patch("music_annotator._pipeline._markup_escape", side_effect=lambda s: s)
        mocker.patch("builtins.input", return_value="s")

        _prompt_collision_policy([result], dest)  # pylint: disable=protected-access

        # The detail string must appear in the per-file output.
        assert any("same AcoustID cluster" in line for line in printed)
        # The match label ("identical") must appear for match=True.
        assert any("identical" in line for line in printed)


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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            return _rec({"id": rec_id, "title": "T", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []})

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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

    def test_resolve_long_names_leaf_suffix_preserved_when_stem_plus_suffix_over(self, fs: FakeFilesystem) -> None:
        """_resolve_long_names preserves the .flac suffix when stem+suffix exceeds _NAME_MAX.

        The stem fits within the limit on its own, but stem+".flac" exceeds it.  Without suffix
        awareness the truncation would eat the extension.  The result must end with ".flac" and
        fit within _NAME_MAX.

        :param fs: pyfakefs fixture.
        """
        # Use real _NAME_MAX (255).  Build a stem that fits alone but not with ".flac" (5 bytes).
        # stem = "01 - " (5) + "A" * 247 = 252 bytes ≤ 255; leaf = 252 + 5 = 257 > 255.
        stem = "01 - " + "A" * 247
        leaf = stem + ".flac"
        assert len(stem.encode("utf-8")) <= 255
        assert len(leaf.encode("utf-8")) > 255

        dest = Path("/dest")
        fs.create_dir(str(dest))
        fs.create_file(str(dest / "dummy.flac"))

        src_file = dest / "dummy.flac"
        dest_file = dest / leaf
        plan = [CopyPlanEntry(idx=0, src_file=src_file, dest_file=dest_file)]

        result = _resolve_long_names(plan, dest, ui=None)
        result_leaf = result[0].dest_file.name
        assert result_leaf.endswith(".flac"), f"leaf must end with .flac, got {result_leaf!r}"
        assert len(result_leaf.encode("utf-8")) <= 255, (
            f"leaf must fit within 255 bytes, got {len(result_leaf.encode('utf-8'))}"
        )

    def test_resolve_long_names_leaf_suffix_preserved_when_stem_already_over(self, fs: FakeFilesystem) -> None:
        """_resolve_long_names preserves the .flac suffix when the stem alone already exceeds _NAME_MAX.

        :param fs: pyfakefs fixture.
        """
        stem = "01 - " + "B" * 260  # 265 bytes > 255
        leaf = stem + ".flac"
        assert len(stem.encode("utf-8")) > 255

        dest = Path("/dest")
        fs.create_dir(str(dest))
        fs.create_file(str(dest / "dummy.flac"))

        src_file = dest / "dummy.flac"
        dest_file = dest / leaf
        plan = [CopyPlanEntry(idx=0, src_file=src_file, dest_file=dest_file)]

        result = _resolve_long_names(plan, dest, ui=None)
        result_leaf = result[0].dest_file.name
        assert result_leaf.endswith(".flac"), f"leaf must end with .flac, got {result_leaf!r}"
        assert len(result_leaf.encode("utf-8")) <= 255, (
            f"leaf must fit within 255 bytes, got {len(result_leaf.encode('utf-8'))}"
        )

    def test_resolve_long_names_trailing_dot_in_stem_not_mistaken_for_extension(self, fs: FakeFilesystem) -> None:
        """_resolve_long_names does not mistake a trailing dot in the work title for the audio extension.

        A leaf like "01 - Sonata op. 23.flac" has ". 23" as Path.suffix — the fix uses the source
        file's suffix directly so "op." is preserved as part of the stem, not treated as the extension.

        :param fs: pyfakefs fixture.
        """
        # Build a leaf whose stem ends in "op. 23" and is long enough to require truncation.
        # "01 - " (5) + "Sonata " * 36 (252) + "op. 23" (6) = 263 bytes stem; leaf = 263 + 5 = 268 > 255.
        stem = "01 - " + "Sonata " * 36 + "op. 23"  # ends in "op. 23", well over 255 bytes
        leaf = stem + ".flac"
        assert len(leaf.encode("utf-8")) > 255

        dest = Path("/dest")
        fs.create_dir(str(dest))
        fs.create_file(str(dest / "dummy.flac"))

        src_file = dest / "dummy.flac"
        dest_file = dest / leaf
        plan = [CopyPlanEntry(idx=0, src_file=src_file, dest_file=dest_file)]

        result = _resolve_long_names(plan, dest, ui=None)
        result_leaf = result[0].dest_file.name
        assert result_leaf.endswith(".flac"), f"leaf must end with .flac, got {result_leaf!r}"
        assert len(result_leaf.encode("utf-8")) <= 255, (
            f"leaf must fit within 255 bytes, got {len(result_leaf.encode('utf-8'))}"
        )


# ---------------------------------------------------------------------------
# run() — TOC-based medium selection via 00 - disc info.yaml
# ---------------------------------------------------------------------------

#: CD frame offsets for a fictional disc 1 (4 tracks).
_DISC1_OFFSETS: list[int] = [182, 50000, 100000, 150000]
#: CD frame offsets for a fictional disc 2 (4 tracks).
_DISC2_OFFSETS: list[int] = [182, 60000, 110000, 160000]

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

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            return _rec(
                {"id": rec_id, "title": "Track", "artist-credit": [], "artist-relation-list": [], "work-relation-list": []}
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

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


# ---------------------------------------------------------------------------
# check_duration_preflight / _prompt_duration_warnings / run() duration guard
# ---------------------------------------------------------------------------


class TestCheckDurationPreflight:
    """Tests for check_duration_preflight — source-vs-MB duration comparison."""

    def _make_track(self, position: int, title: str, length_ms: int) -> MBTrack:
        """Build a minimal MBTrack with a specific position, title, and length.

        :param position: 1-based track position.
        :param title: Recording title string.
        :param length_ms: Track length in milliseconds.
        :returns: An :class:`~music_annotator.models.MBTrack` instance.
        """
        return MBTrack.model_validate(
            {
                "id": f"trk-{position}",
                "position": position,
                "length": str(length_ms),
                "recording": {"id": f"rec-{position}", "title": title, "artist-credit": []},
            }
        )

    def test_all_within_tolerance_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """No warnings when all source durations are within the tolerance of MB lengths.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src1 = Path("/src/01.flac")
        src2 = Path("/src/02.flac")
        fs.create_file(str(src1), contents=_MINIMAL_FLAC)
        fs.create_file(str(src2), contents=_MINIMAL_FLAC)
        mocker.patch("music_annotator._pipeline_io._read_duration_ms", side_effect=[60_000, 120_000])
        track_pairs = [
            (self._make_track(1, "Movement I", 60_500), 1),  # delta 500 ms — within 10 s
            (self._make_track(2, "Movement II", 120_200), 1),  # delta 200 ms — within 10 s
        ]
        result = check_duration_preflight([src1, src2], track_pairs)
        assert result == []

    def test_one_track_over_tolerance_returns_one_warning(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """One warning is returned when exactly one track exceeds the tolerance.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src1 = Path("/src/01.flac")
        src2 = Path("/src/02.flac")
        fs.create_file(str(src1), contents=_MINIMAL_FLAC)
        fs.create_file(str(src2), contents=_MINIMAL_FLAC)
        # Track 1: source 30 s, MB 60 s — delta 30 000 ms exceeds 10 000 ms tolerance.
        # Track 2: source 120 s, MB 120.5 s — delta 500 ms within tolerance.
        mocker.patch("music_annotator._pipeline_io._read_duration_ms", side_effect=[30_000, 120_000])
        track_pairs = [
            (self._make_track(1, "Symphony no. 1: I. Allegro", 60_000), 1),
            (self._make_track(2, "Symphony no. 1: II. Andante", 120_500), 1),
        ]
        result = check_duration_preflight([src1, src2], track_pairs)
        assert len(result) == 1
        assert "track 1" in result[0]
        assert "Symphony no. 1: I. Allegro" in result[0]
        # source 30.0s, MB 60.0s, delta 30.0s
        assert "30.0s" in result[0]
        assert "60.0s" in result[0]

    def test_mb_length_zero_skipped(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Tracks with MB length of 0 are silently skipped even when source duration differs greatly.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/01.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        mocker.patch("music_annotator._pipeline_io._read_duration_ms", return_value=300_000)
        # MBTrack with length=0 (MB has no duration data for this recording).
        track_pairs = [(self._make_track(1, "Unknown Recording", 0), 1)]
        result = check_duration_preflight([src], track_pairs)
        assert result == []

    def test_src_length_zero_skipped(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Tracks where mutagen returns 0 for source duration are silently skipped.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/01.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        mocker.patch("music_annotator._pipeline_io._read_duration_ms", return_value=0)
        track_pairs = [(self._make_track(1, "Track One", 60_000), 1)]
        result = check_duration_preflight([src], track_pairs)
        assert result == []

    def test_all_tracks_over_tolerance_returns_all_warnings(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """One warning per track when every track exceeds the tolerance.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src1 = Path("/src/01.flac")
        src2 = Path("/src/02.flac")
        src3 = Path("/src/03.flac")
        fs.create_file(str(src1), contents=_MINIMAL_FLAC)
        fs.create_file(str(src2), contents=_MINIMAL_FLAC)
        fs.create_file(str(src3), contents=_MINIMAL_FLAC)
        # Each source is ~2 minutes shorter than the MB length — clearly wrong MBID.
        mocker.patch(
            "music_annotator._pipeline_io._read_duration_ms",
            side_effect=[30_000, 30_000, 30_000],
        )
        track_pairs = [
            (self._make_track(1, "Movement I", 150_000), 1),
            (self._make_track(2, "Movement II", 150_000), 1),
            (self._make_track(3, "Movement III", 150_000), 1),
        ]
        result = check_duration_preflight([src1, src2, src3], track_pairs)
        assert len(result) == 3

    def test_custom_tolerance_respected(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """A custom tolerance_ms parameter gates warnings at the specified threshold.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/01.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        # Delta is 3 000 ms; within the default 10 000 ms but above a 2 000 ms custom tolerance.
        mocker.patch("music_annotator._pipeline_io._read_duration_ms", return_value=57_000)
        track_pairs = [(self._make_track(1, "Track One", 60_000), 1)]

        result_wide = check_duration_preflight([src], track_pairs, tolerance_ms=10_000)
        assert result_wide == []

        mocker.patch("music_annotator._pipeline_io._read_duration_ms", return_value=57_000)
        result_tight = check_duration_preflight([src], track_pairs, tolerance_ms=2_000)
        assert len(result_tight) == 1

    def test_warning_message_contains_position_title_and_durations(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Warning message includes track position, title, source seconds, MB seconds, and delta seconds.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/01.flac")
        fs.create_file(str(src), contents=_MINIMAL_FLAC)
        mocker.patch("music_annotator._pipeline_io._read_duration_ms", return_value=45_000)
        track_pairs = [(self._make_track(3, "Largo", 90_000), 2)]
        result = check_duration_preflight([src], track_pairs)
        assert len(result) == 1
        msg = result[0]
        assert "track 3" in msg
        assert "Largo" in msg
        assert "45.0s" in msg  # source duration
        assert "90.0s" in msg  # MB duration
        assert "45.0s" in msg  # delta (90 - 45 = 45)


class TestPromptDurationWarnings:
    """Tests for _prompt_duration_warnings — interactive duration mismatch prompt."""

    def test_user_proceeds_returns_true(self, mocker: MockerFixture) -> None:
        """Returns True when the user enters 'p' to proceed.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._pipeline._console.print")
        mocker.patch("builtins.input", return_value="p")
        assert _prompt_duration_warnings(["  track 1 'Allegro': source 30.0s, MB 60.0s (delta 30.0s)"]) is True

    def test_user_proceed_word_returns_true(self, mocker: MockerFixture) -> None:
        """Returns True when the user enters the full word 'proceed'.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._pipeline._console.print")
        mocker.patch("builtins.input", return_value="proceed")
        assert _prompt_duration_warnings(["  track 1 'Allegro': source 30.0s, MB 60.0s (delta 30.0s)"]) is True

    def test_user_aborts_returns_false(self, mocker: MockerFixture) -> None:
        """Returns False when the user enters 'a' to abort.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._pipeline._console.print")
        mocker.patch("builtins.input", return_value="a")
        assert _prompt_duration_warnings(["  track 1 'Allegro': source 30.0s, MB 60.0s (delta 30.0s)"]) is False

    def test_user_abort_word_returns_false(self, mocker: MockerFixture) -> None:
        """Returns False when the user enters the full word 'abort'.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._pipeline._console.print")
        mocker.patch("builtins.input", return_value="abort")
        assert _prompt_duration_warnings(["  track 1 'Allegro': source 30.0s, MB 60.0s (delta 30.0s)"]) is False

    def test_invalid_then_valid_input_reprompts(self, mocker: MockerFixture) -> None:
        """Invalid input causes a re-prompt; subsequent valid input is accepted.

        :param mocker: pytest-mock fixture.
        """
        mocker.patch("music_annotator._pipeline._console.print")
        mocker.patch("builtins.input", side_effect=["x", "bad", "p"])
        assert _prompt_duration_warnings(["  track 1 'Allegro': source 30.0s, MB 60.0s (delta 30.0s)"]) is True

    def test_warnings_displayed_in_output(self, mocker: MockerFixture) -> None:
        """Each warning string is printed to the console.

        :param mocker: pytest-mock fixture.
        """
        printed: list[str] = []
        mocker.patch("music_annotator._pipeline._console.print", side_effect=lambda s, **_: printed.append(s))
        mocker.patch("builtins.input", return_value="a")
        warnings = [
            "  track 1 'Allegro': source 30.0s, MB 60.0s (delta 30.0s)",
            "  track 3 'Largo': source 45.0s, MB 90.0s (delta 45.0s)",
        ]
        _prompt_duration_warnings(warnings)
        assert any("30.0s" in line for line in printed)
        assert any("45.0s" in line for line in printed)

    def test_warning_count_shown_in_header(self, mocker: MockerFixture) -> None:
        """The header line includes the number of mismatched tracks.

        :param mocker: pytest-mock fixture.
        """
        printed: list[str] = []
        mocker.patch("music_annotator._pipeline._console.print", side_effect=lambda s, **_: printed.append(s))
        mocker.patch("builtins.input", return_value="p")
        _prompt_duration_warnings(["w1", "w2", "w3"])
        assert any("3" in line for line in printed)


class TestRunDurationPreflight:
    """Tests for the duration pre-flight check wired into run()."""

    def _patch_mb(self, mocker: MockerFixture, release: MBRelease) -> None:
        """Patch all MB API and tagging calls for a run() invocation.

        :param mocker: pytest-mock fixture.
        :param release: Release model to return from fetch_release.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        mocker.patch(
            "music_annotator._pipeline.fetch_recording_detail",
            return_value=_rec({"id": "rec-1", "title": "Track", "artist-credit": [], "work-relation-list": []}),
        )
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

    def test_no_warnings_no_prompt(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When check_duration_preflight returns an empty list, no prompt is shown.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        self._patch_mb(mocker, _make_release(n_tracks=1))
        mocker.patch("music_annotator._pipeline.check_duration_preflight", return_value=[])
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

    def test_warnings_present_user_proceeds(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When duration warnings exist and the user enters 'p', run() proceeds normally.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        self._patch_mb(mocker, _make_release(n_tracks=1))
        mocker.patch(
            "music_annotator._pipeline.check_duration_preflight",
            return_value=["  track 1 'Track 1': source 30.0s, MB 60.0s (delta 30.0s)"],
        )
        mocker.patch("music_annotator._pipeline._console.print")
        mocker.patch("builtins.input", return_value="p")
        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )
        # Pipeline completed — tagging was called.
        assert mock_tag.call_count == 1

    def test_warnings_present_user_aborts_raises_system_exit(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When duration warnings exist and the user enters 'a', run() raises SystemExit(1).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        self._patch_mb(mocker, _make_release(n_tracks=1))
        mocker.patch(
            "music_annotator._pipeline.check_duration_preflight",
            return_value=["  track 1 'Track 1': source 30.0s, MB 60.0s (delta 30.0s)"],
        )
        mocker.patch("music_annotator._pipeline._console.print")
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

    def test_dry_run_skips_preflight_entirely(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """In dry_run mode, check_duration_preflight is never called.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        self._patch_mb(mocker, _make_release(n_tracks=1))
        mock_preflight = mocker.patch("music_annotator._pipeline.check_duration_preflight")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=True,
            fetch_rels=False,
        )
        mock_preflight.assert_not_called()


# ---------------------------------------------------------------------------
# _audio_hash — KAT C-F0c: tagging-invariant decoded-audio hash
# ---------------------------------------------------------------------------


class TestAudioHashInvariantAcrossTagging:
    """KAT test_audio_hash_invariant_across_tagging: _audio_hash is stable before and after tagging.

    Verifies that the algorithm-tagged hash returned by :func:`_audio_hash` is unchanged after
    :func:`apply_tags_flac` / :func:`apply_tags_mp3` modify the container metadata, confirming
    the hash is tagging-invariant.  Also exercises the unsupported-suffix arm (returns ``""``).
    """

    def test_flac_hash_stable_across_tagging(self, fs: FakeFilesystem) -> None:
        """_audio_hash on a FLAC file is unchanged after apply_tags_flac writes new tags.

        The FLAC STREAMINFO MD5 reflects only the decoded PCM audio; Vorbis Comment and PICTURE
        blocks do not affect it.  The hash must therefore be identical before and after tagging.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.flac")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_FLAC)

        hash_before = _audio_hash(dest)
        apply_tags_flac(dest, TrackTags(title="Tagged Title", album="Album", tracknumber="1"))
        hash_after = _audio_hash(dest)

        assert hash_before == hash_after
        assert hash_before.startswith("flac-md5:")
        assert hash_before != ""

    def test_mp3_hash_stable_across_tagging(self, fs: FakeFilesystem) -> None:
        """_audio_hash on an MP3 file is unchanged after apply_tags_mp3 writes new ID3 tags.

        The MP3 hash covers only the raw audio-frame bytes (from the end of the ID3v2 header to
        EOF, minus any trailing ID3v1 tag).  Rewriting the ID3v2 header changes its size, but
        _audio_hash recomputes the boundary on each call, so the audio-frame bytes — and therefore
        the hash — remain identical.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=_MINIMAL_MP3)

        hash_before = _audio_hash(dest)
        apply_tags_mp3(dest, TrackTags(title="Tagged Title", album="Album", tracknumber="1"))
        hash_after = _audio_hash(dest)

        assert hash_before == hash_after
        assert hash_before.startswith("mp3-stream-sha256:")
        assert hash_before != ""

    def test_unsupported_suffix_returns_empty_string(self, fs: FakeFilesystem) -> None:
        """_audio_hash returns '' for file extensions not supported by the hash function.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/out/track.ogg")
        fs.create_dir("/out")
        fs.create_file(str(dest), contents=b"OggS" + b"\x00" * 50)

        assert _audio_hash(dest) == ""

    def test_mp3_hash_strips_trailing_id3v1_tag(self, fs: FakeFilesystem) -> None:
        """_audio_hash strips a trailing ID3v1 tag (128 bytes starting with b'TAG') before hashing.

        An MP3 with a trailing ID3v1 tag must produce the same hash as the same audio bytes
        without the ID3v1 tag, confirming the stripping branch is exercised.

        :param fs: pyfakefs fixture.
        """
        # Build an MP3 with a trailing ID3v1 tag appended after the audio frames.
        id3v1_tag = b"TAG" + b"\x00" * 125  # exactly 128 bytes
        mp3_with_id3v1 = _MINIMAL_MP3 + id3v1_tag
        mp3_without_id3v1 = _MINIMAL_MP3

        dest_with = Path("/out/with_id3v1.mp3")
        dest_without = Path("/out/without_id3v1.mp3")
        fs.create_dir("/out")
        fs.create_file(str(dest_with), contents=mp3_with_id3v1)
        fs.create_file(str(dest_without), contents=mp3_without_id3v1)

        hash_with = _audio_hash(dest_with)
        hash_without = _audio_hash(dest_without)

        # Both must produce the same hash — the ID3v1 tag is stripped before hashing.
        assert hash_with == hash_without
        assert hash_with.startswith("mp3-stream-sha256:")

    def test_audio_hash_returns_empty_on_read_error(self) -> None:
        """_audio_hash returns '' when the file cannot be read (exception path)."""
        dest = Path("/nonexistent/path/missing.flac")
        # File does not exist — FLAC() will raise an exception, caught by the bare except.
        assert _audio_hash(dest) == ""


# ---------------------------------------------------------------------------
# F1: audio_hash written to tag and journal
# ---------------------------------------------------------------------------


class TestIngestAudioHash:
    """Tests for F1: audio_hash computed from source and written to the FLAC tag and journal entry.

    Uses the real apply_tags_flac and _verify_copy (not mocked) so the full write-and-read-back
    path executes.  _audio_hash is also real: _MINIMAL_FLAC has an all-zero STREAMINFO MD5, so
    the expected hash is "flac-md5:00000000000000000000000000000000".
    """

    def _patch_mb(self, mocker: MockerFixture, release: MBRelease) -> None:
        """Patch all MB API calls for a single-track run.

        Does NOT patch apply_tags_flac or _verify_copy so the real tagging and verification
        path executes.

        :param mocker: pytest-mock fixture.
        :param release: Release model to return from fetch_release.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        mocker.patch(
            "music_annotator._pipeline.fetch_recording_detail",
            return_value=_rec(
                {
                    "id": "rec-1",
                    "title": "Track 1",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [],
                }
            ),
        )
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

    def test_ingest_writes_audio_hash_tag_and_journal(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """After run(), the destination FLAC has a non-empty AUDIO_HASH tag and the journal entry carries it.

        Verifies both halves of the F1 contract:

        (a) The destination FLAC file has an ``AUDIO_HASH`` Vorbis Comment tag that is non-empty
            and starts with ``"flac-md5:"``.
        (b) The corresponding ``action="tagged"`` journal entry has an ``audio_hash`` field that
            matches the tag value.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        # _MINIMAL_FLAC has an all-zero STREAMINFO MD5, so _audio_hash returns
        # "flac-md5:00000000000000000000000000000000".
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

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

        # (a) Verify the destination FLAC has a non-empty AUDIO_HASH tag starting with "flac-md5:".
        dest_flac_files = list(dest.rglob("*.flac"))
        assert len(dest_flac_files) == 1, f"Expected 1 FLAC in dest, found {len(dest_flac_files)}"
        dest_flac = dest_flac_files[0]
        audio = FLAC(str(dest_flac))
        audio_hash_values = audio.get("audio_hash") or []
        assert audio_hash_values, "AUDIO_HASH tag is missing from destination FLAC"
        audio_hash_tag = audio_hash_values[0]
        assert audio_hash_tag.startswith("flac-md5:"), f"AUDIO_HASH tag does not start with 'flac-md5:': {audio_hash_tag!r}"
        assert audio_hash_tag != "", "AUDIO_HASH tag is empty"

        # (b) Verify the journal entry carries the matching audio_hash field.
        journal_path = dest / JOURNAL_FILENAME
        assert journal_path.exists(), "Journal file was not written"
        data = json.loads(journal_path.read_text(encoding="utf-8"))
        tagged_entries = [e for e in data if e.get("action") == "tagged"]
        assert len(tagged_entries) == 1, f"Expected 1 tagged journal entry, found {len(tagged_entries)}"
        journal_audio_hash = tagged_entries[0].get("audio_hash", "")
        assert journal_audio_hash == audio_hash_tag, (
            f"Journal audio_hash {journal_audio_hash!r} does not match tag {audio_hash_tag!r}"
        )


# ---------------------------------------------------------------------------
# F3: acoustid_fingerprint written to tag and journal
# ---------------------------------------------------------------------------


class TestIngestAcoustidFingerprint:
    """Tests for F3: acoustid_fingerprint computed from source and stored in the journal entry.

    Uses the real apply_tags_flac and _verify_copy (not mocked) so the full write-and-read-back
    path executes.  _run_fpcalc is mocked because fpcalc is not available in the test environment.
    """

    def _patch_mb(self, mocker: MockerFixture, release: MBRelease) -> None:
        """Patch all MB API calls for a single-track run.

        Does NOT patch apply_tags_flac or _verify_copy so the real tagging and verification
        path executes.

        :param mocker: pytest-mock fixture.
        :param release: Release model to return from fetch_release.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        mocker.patch(
            "music_annotator._pipeline.fetch_recording_detail",
            return_value=_rec(
                {
                    "id": "rec-1",
                    "title": "Track 1",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [],
                }
            ),
        )
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

    def test_ingest_writes_acoustid_fingerprint_to_journal(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """After run(), the journal entry carries the acoustid_fingerprint field from _run_fpcalc.

        Verifies the F3 contract:

        (a) The ``action="tagged"`` journal entry has an ``acoustid_fingerprint`` field that matches
            the value returned by ``_run_fpcalc``.
        (b) The ``acoustid_fingerprint`` field on the ``TrackTags`` passed to ``apply_tags_flac``
            matches the mocked fingerprint value.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        # Construct a valid base64url fingerprint to return from the mocked _run_fpcalc.
        fp_bytes = struct.pack("<4I", 0x12345678, 0xABCDEF01, 0x87654321, 0x10FEDCBA)
        expected_fp = base64.b64encode(fp_bytes).decode().rstrip("=").replace("+", "-").replace("/", "_")

        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=1)
        self._patch_mb(mocker, release)
        # Mock _run_fpcalc in the pipeline module (where it is imported).
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value=expected_fp)

        mock_tag = mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        # Note: _run_fpcalc is already mocked above with expected_fp; do NOT add a second mock here.

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        # (a) Verify the journal entry carries the acoustid_fingerprint field.
        journal_path = dest / JOURNAL_FILENAME
        assert journal_path.exists(), "Journal file was not written"
        data = json.loads(journal_path.read_text(encoding="utf-8"))
        tagged_entries = [e for e in data if e.get("action") == "tagged"]
        assert len(tagged_entries) == 1, f"Expected 1 tagged journal entry, found {len(tagged_entries)}"
        journal_fp = tagged_entries[0].get("acoustid_fingerprint", "")
        assert journal_fp == expected_fp, f"Journal acoustid_fingerprint {journal_fp!r} does not match expected {expected_fp!r}"

        # (b) Verify the TrackTags passed to apply_tags_flac carries the fingerprint.
        tags_used: TrackTags = mock_tag.call_args[0][1]
        assert tags_used.acoustid_fingerprint == expected_fp, (
            f"TrackTags.acoustid_fingerprint {tags_used.acoustid_fingerprint!r} does not match expected {expected_fp!r}"
        )

    def test_ingest_empty_fp_when_fpcalc_unavailable(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """When _run_fpcalc returns '' (fpcalc unavailable), acoustid_fingerprint is '' in journal.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs filesystem fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=1)
        self._patch_mb(mocker, release)
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        journal_path = dest / JOURNAL_FILENAME
        data = json.loads(journal_path.read_text(encoding="utf-8"))
        tagged_entries = [e for e in data if e.get("action") == "tagged"]
        assert len(tagged_entries) == 1
        # acoustid_fingerprint should be "" (empty string, the default) when fpcalc is unavailable.
        journal_fp = tagged_entries[0].get("acoustid_fingerprint", "NOT_PRESENT")
        assert journal_fp == "", f"Expected empty acoustid_fingerprint, got {journal_fp!r}"


# ---------------------------------------------------------------------------
# ISRC identity rung — _read_isrc_tag and _isrc_matches
# ---------------------------------------------------------------------------


class TestIsrcIdentityRung:
    """Tests for the ISRC identity rung: _read_isrc_tag and _isrc_matches."""

    def test_read_isrc_tag_flac_returns_value(self, fs: FakeFilesystem) -> None:
        """_read_isrc_tag reads the ISRC Vorbis Comment from a FLAC file.

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.flac")
        fs.create_dir("/out")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)
        tags = TrackTags(
            isrc="GBAYE0000001", movementnumber="1", movementtotal="1", cea_conductors_list=[], cea_ensembles_list=[]
        )
        apply_tags_flac(path, tags)
        assert _read_isrc_tag(path) == "GBAYE0000001"

    def test_read_isrc_tag_flac_no_tag_returns_empty(self, fs: FakeFilesystem) -> None:
        """_read_isrc_tag returns '' for a FLAC file with no ISRC tag.

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.flac")
        fs.create_dir("/out")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)
        apply_tags_flac(
            path, TrackTags(title="X", movementnumber="1", movementtotal="1", cea_conductors_list=[], cea_ensembles_list=[])
        )
        assert _read_isrc_tag(path) == ""

    def test_read_isrc_tag_mp3_returns_value(self, fs: FakeFilesystem) -> None:
        """_read_isrc_tag reads the ISRC from the TSRC frame of an MP3 file.

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(path), contents=_MINIMAL_MP3)
        tags = TrackTags(
            isrc="GBAYE0000001", movementnumber="1", movementtotal="1", cea_conductors_list=[], cea_ensembles_list=[]
        )
        apply_tags_mp3(path, tags)
        assert _read_isrc_tag(path) == "GBAYE0000001"

    def test_read_isrc_tag_mp3_no_tsrc_returns_empty(self, fs: FakeFilesystem) -> None:
        """_read_isrc_tag returns '' for an MP3 file with no TSRC frame.

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(path), contents=_MINIMAL_MP3)
        apply_tags_mp3(
            path, TrackTags(title="X", movementnumber="1", movementtotal="1", cea_conductors_list=[], cea_ensembles_list=[])
        )
        assert _read_isrc_tag(path) == ""

    def test_read_isrc_tag_unsupported_extension_returns_empty(self, fs: FakeFilesystem) -> None:
        """_read_isrc_tag returns '' for unsupported file extensions.

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.wav")
        fs.create_file(str(path), contents=b"RIFF")
        assert _read_isrc_tag(path) == ""

    def test_read_isrc_tag_exception_returns_empty(self, fs: FakeFilesystem) -> None:
        """_read_isrc_tag returns '' when mutagen raises an exception (corrupt file).

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.flac")
        fs.create_file(str(path), contents=b"not a real flac")
        assert _read_isrc_tag(path) == ""

    def test_isrc_match_resolves_identity(self, fs: FakeFilesystem) -> None:
        """_isrc_matches returns match=True, method='isrc' when source ISRC matches candidate isrc_list.

        :param fs: pyfakefs fixture.
        """
        path = Path("/src/track.flac")
        fs.create_dir("/src")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)
        tags = TrackTags(
            isrc="GBAYE0000001", movementnumber="1", movementtotal="1", cea_conductors_list=[], cea_ensembles_list=[]
        )
        apply_tags_flac(path, tags)

        result = _isrc_matches(path, ["GBAYE0000001", "USRC10000001"])
        assert result.match is True
        assert result.method == "isrc"
        assert "GBAYE0000001" in result.detail

    def test_isrc_no_match_returns_false(self, fs: FakeFilesystem) -> None:
        """_isrc_matches returns match=False when source ISRC does not appear in candidate isrc_list.

        :param fs: pyfakefs fixture.
        """
        path = Path("/src/track.flac")
        fs.create_dir("/src")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)
        tags = TrackTags(
            isrc="GBAYE0000001", movementnumber="1", movementtotal="1", cea_conductors_list=[], cea_ensembles_list=[]
        )
        apply_tags_flac(path, tags)

        result = _isrc_matches(path, ["USRC10000001", "USRC10000002"])
        assert result.match is False
        assert result.method == "isrc"

    def test_isrc_empty_isrc_list_returns_inconclusive(self, fs: FakeFilesystem) -> None:
        """_isrc_matches returns match=None when the candidate isrc_list is empty.

        :param fs: pyfakefs fixture.
        """
        path = Path("/src/track.flac")
        fs.create_dir("/src")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)
        tags = TrackTags(
            isrc="GBAYE0000001", movementnumber="1", movementtotal="1", cea_conductors_list=[], cea_ensembles_list=[]
        )
        apply_tags_flac(path, tags)

        result = _isrc_matches(path, [])
        assert result.match is None
        assert result.method == "isrc"

    def test_isrc_no_tag_in_source_returns_inconclusive(self, fs: FakeFilesystem) -> None:
        """_isrc_matches returns match=None when the source file has no ISRC tag.

        :param fs: pyfakefs fixture.
        """
        path = Path("/src/track.flac")
        fs.create_dir("/src")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)
        apply_tags_flac(
            path, TrackTags(title="X", movementnumber="1", movementtotal="1", cea_conductors_list=[], cea_ensembles_list=[])
        )

        result = _isrc_matches(path, ["GBAYE0000001"])
        assert result.match is None
        assert result.method == "isrc"


# ---------------------------------------------------------------------------
# run() — AcoustID identity-confirm block
# ---------------------------------------------------------------------------


class TestRunAcoustidIdentityConfirm:
    """Tests for the AcoustID identity-confirm block in run().

    The identity-confirm block is a read-only diagnostic step that logs whether the selected
    recording MBID is confirmed or contradicted by the AcoustID lookup results.  It never alters
    the copy/tag/verify path.
    """

    def _patch_mb(self, mocker: MockerFixture, release: MBRelease) -> None:
        """Patch all MB API calls and post-copy verification for run() tests.

        :param mocker: pytest-mock fixture.
        :param release: MBRelease model to return from fetch_release.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
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

        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="test-fingerprint")

    def test_acoustid_confirm_ok_logged_when_recording_in_results(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """acoustid_confirm_ok is logged when the selected recording MBID is in the AcoustID results.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=1)
        rec_id = release.medium_list[0].track_list[0].recording.id
        self._patch_mb(mocker, release)
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._fetch_acoustid_lookup_raw", return_value=([rec_id], "uuid-1"))
        mocker.patch("music_annotator._pipeline._read_duration_ms", return_value=180000)

        log_events: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._pipeline.log.info",
            side_effect=lambda event, **kw: log_events.append({"event": event, **kw}),
        )

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            acoustid_key="my-api-key",
        )
        assert any(e["event"] == "acoustid_confirm_ok" for e in log_events)

    def test_acoustid_confirm_mismatch_logged_when_recording_not_in_results(
        self, mocker: MockerFixture, fs: FakeFilesystem
    ) -> None:
        """acoustid_confirm_mismatch is logged when the selected recording MBID is not in the AcoustID results.

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
        mocker.patch("music_annotator._pipeline._fetch_acoustid_lookup_raw", return_value=(["other-mbid"], "uuid-1"))
        mocker.patch("music_annotator._pipeline._read_duration_ms", return_value=180000)

        log_events: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._pipeline.log.warning",
            side_effect=lambda event, **kw: log_events.append({"event": event, **kw}),
        )

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            acoustid_key="my-api-key",
        )
        assert any(e["event"] == "acoustid_confirm_mismatch" for e in log_events)

    def test_noop_when_acoustid_key_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """fetch_acoustid_lookup is not called when acoustid_key == ''.

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
        mock_lookup = mocker.patch("music_annotator._pipeline._fetch_acoustid_lookup_raw")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            acoustid_key="",
        )
        mock_lookup.assert_not_called()

    def test_noop_when_acoustid_fingerprint_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_fetch_acoustid_lookup_raw is not called when acoustid_fingerprint == '' (fpcalc unavailable).

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
        # Override _run_fpcalc to return empty string (fpcalc unavailable)
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mock_lookup = mocker.patch("music_annotator._pipeline._fetch_acoustid_lookup_raw")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            acoustid_key="my-api-key",
        )
        mock_lookup.assert_not_called()

    def test_noop_when_lookup_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """No log event when _fetch_acoustid_lookup_raw returns ([], '') (covers empty-results branch).

        When _confirm_mbids is empty, the ``if _confirm_mbids and _selected_rec_id:`` condition
        is False and neither acoustid_confirm_ok nor acoustid_confirm_mismatch is logged.

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
        mocker.patch("music_annotator._pipeline._fetch_acoustid_lookup_raw", return_value=([], ""))
        mocker.patch("music_annotator._pipeline._read_duration_ms", return_value=180000)

        log_info_events: list[dict[str, object]] = []
        log_warn_events: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._pipeline.log.info",
            side_effect=lambda event, **kw: log_info_events.append({"event": event, **kw}),
        )
        mocker.patch(
            "music_annotator._pipeline.log.warning",
            side_effect=lambda event, **kw: log_warn_events.append({"event": event, **kw}),
        )

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
            acoustid_key="my-api-key",
        )
        assert not any(e["event"] == "acoustid_confirm_ok" for e in log_info_events)
        assert not any(e["event"] == "acoustid_confirm_mismatch" for e in log_warn_events)


# ---------------------------------------------------------------------------
# enrich_origin_time helpers
# ---------------------------------------------------------------------------


class TestCollectWorkDirProvenance:
    """Tests for :func:`music_annotator._pipeline_io._collect_work_dir_provenance`."""

    def test_groups_by_work_top_dir(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """Entries for the same work_top_dir are grouped and the earliest timestamp wins.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        entries = [
            TransactionEntry(
                timestamp="2024-06-02T10:00:00+00:00",
                release_id="rel-1",
                source="/rip/Beethoven/01.flac",
                destination="/lib/Beethoven/Symphony No 5/01 - I.flac",
                action="tagged",
            ),
            TransactionEntry(
                timestamp="2024-06-01T08:00:00+00:00",
                release_id="rel-1",
                source="/rip/Beethoven/02.flac",
                destination="/lib/Beethoven/Symphony No 5/02 - II.flac",
                action="tagged",
            ),
        ]
        journal = TransactionLog(entries=entries)
        result = _collect_work_dir_provenance(dest_root, journal)

        work_top_dir = Path("/lib/Beethoven/Symphony No 5")
        assert work_top_dir in result
        prov = result[work_top_dir]
        # Earliest timestamp wins
        assert prov.origin_time == "2024-06-01T08:00:00+00:00"
        # origin_source is parent of the source with the earliest timestamp
        assert prov.origin_source == "/rip/Beethoven"

    def test_skips_non_tagged_entries(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """Only ``action == "tagged"`` entries are included; others are skipped.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        entries = [
            TransactionEntry(
                timestamp="2024-06-01T00:00:00+00:00",
                release_id="rel-1",
                source="/rip/01.flac",
                destination="/lib/Composer/Work/01.flac",
                action="repathed",
            ),
            TransactionEntry(
                timestamp="2024-06-01T00:00:00+00:00",
                release_id="rel-1",
                source="/rip/01.flac",
                destination="/lib/Composer/Work/01.flac",
                action="enriched",
            ),
        ]
        journal = TransactionLog(entries=entries)
        result = _collect_work_dir_provenance(dest_root, journal)
        assert result == {}

    def test_skips_entries_not_under_dest_root(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """Entries whose destination is not under dest_root are silently skipped.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        entries = [
            TransactionEntry(
                timestamp="2024-06-01T00:00:00+00:00",
                release_id="rel-1",
                source="/rip/01.flac",
                destination="/other/Composer/Work/01.flac",
                action="tagged",
            ),
        ]
        journal = TransactionLog(entries=entries)
        result = _collect_work_dir_provenance(dest_root, journal)
        assert result == {}

    def test_skips_entries_with_too_few_path_parts(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """Entries whose relative path has fewer than two parts are silently skipped.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        entries = [
            TransactionEntry(
                timestamp="2024-06-01T00:00:00+00:00",
                release_id="rel-1",
                source="/rip/01.flac",
                destination="/lib/only-one-part.flac",
                action="tagged",
            ),
        ]
        journal = TransactionLog(entries=entries)
        result = _collect_work_dir_provenance(dest_root, journal)
        assert result == {}


class TestReadProvenanceSidecar:
    """Tests for :func:`music_annotator._pipeline_io._read_provenance_sidecar`."""

    def test_reads_existing_sidecar(self, fs: FakeFilesystem) -> None:
        """Reads origin_time and origin_source from an existing YAML sidecar.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/freedb_disc_1.yaml")
        fs.create_dir(str(sidecar.parent))
        sidecar.write_text(
            "origin_time: '2024-06-01T00:00:00+00:00'\norigin_source: /rip/Beethoven\n",
            encoding="utf-8",
        )
        result = _read_provenance_sidecar(sidecar)
        assert result.origin_time == "2024-06-01T00:00:00+00:00"
        assert result.origin_source == "/rip/Beethoven"

    def test_absent_file_returns_empty(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """Returns an empty ProvenanceSidecar when the file does not exist.

        :param fs: pyfakefs fixture.
        """
        result = _read_provenance_sidecar(Path("/lib/Composer/Work/freedb_disc_1.yaml"))
        assert result == ProvenanceSidecar()

    def test_non_dict_yaml_returns_empty(self, fs: FakeFilesystem) -> None:
        """Returns an empty ProvenanceSidecar when the YAML content is not a mapping.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/freedb_disc_1.yaml")
        fs.create_dir(str(sidecar.parent))
        sidecar.write_text("- item1\n- item2\n", encoding="utf-8")
        result = _read_provenance_sidecar(sidecar)
        assert result == ProvenanceSidecar()

    def test_read_error_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Returns an empty ProvenanceSidecar when the file exists but raises on read.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/freedb_disc_1.yaml")
        fs.create_dir(str(sidecar.parent))
        sidecar.write_text("origin_time: '2024-06-01'\n", encoding="utf-8")
        mocker.patch("music_annotator._pipeline_io.yaml.full_load", side_effect=OSError("read error"))
        result = _read_provenance_sidecar(sidecar)
        assert result == ProvenanceSidecar()


class TestWriteProvenanceFields:
    """Tests for :func:`music_annotator._pipeline_io._write_provenance_fields`."""

    def test_creates_new_sidecar(self, fs: FakeFilesystem) -> None:
        """Creates a new YAML sidecar with origin_time and origin_source when none exists.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/music_annotator_provenance.yaml")
        fs.create_dir(str(sidecar.parent))
        provenance = ProvenanceSidecar(origin_time="2024-06-01T00:00:00+00:00", origin_source="/rip/Beethoven")
        _write_provenance_fields(sidecar, provenance)
        result = _read_provenance_sidecar(sidecar)
        assert result.origin_time == "2024-06-01T00:00:00+00:00"
        assert result.origin_source == "/rip/Beethoven"

    def test_merges_into_existing_sidecar(self, fs: FakeFilesystem) -> None:
        """Merges provenance fields into an existing YAML sidecar, preserving other keys.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/freedb_disc_1.yaml")
        fs.create_dir(str(sidecar.parent))
        sidecar.write_text("disc_id: [12345, 2, 150, 300, 600]\n", encoding="utf-8")
        provenance = ProvenanceSidecar(origin_time="2024-06-01T00:00:00+00:00", origin_source="/rip/Beethoven")
        _write_provenance_fields(sidecar, provenance)
        with sidecar.open(encoding="utf-8") as fh:
            data: object = yaml.full_load(fh)
        assert isinstance(data, dict)
        assert data["origin_time"] == "2024-06-01T00:00:00+00:00"
        assert data["origin_source"] == "/rip/Beethoven"
        # Original key preserved
        assert "disc_id" in data

    def test_overwrites_existing_provenance_fields(self, fs: FakeFilesystem) -> None:
        """Overwrites existing origin_time and origin_source when called again.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/freedb_disc_1.yaml")
        fs.create_dir(str(sidecar.parent))
        sidecar.write_text(
            "origin_time: '2024-01-01T00:00:00+00:00'\norigin_source: /old/path\n",
            encoding="utf-8",
        )
        provenance = ProvenanceSidecar(origin_time="2024-06-01T00:00:00+00:00", origin_source="/new/path")
        _write_provenance_fields(sidecar, provenance)
        result = _read_provenance_sidecar(sidecar)
        assert result.origin_time == "2024-06-01T00:00:00+00:00"
        assert result.origin_source == "/new/path"

    def test_non_dict_existing_yaml_treated_as_empty(self, fs: FakeFilesystem) -> None:
        """When the existing YAML is not a dict, it is treated as empty and overwritten.

        Covers the ``if isinstance(raw, dict):`` False branch in _write_provenance_fields.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/freedb_disc_1.yaml")
        fs.create_dir(str(sidecar.parent))
        # Write a YAML list (not a dict) as the existing content
        sidecar.write_text("- item1\n- item2\n", encoding="utf-8")
        provenance = ProvenanceSidecar(origin_time="2024-06-01T00:00:00+00:00", origin_source="/rip/Beethoven")
        _write_provenance_fields(sidecar, provenance)
        with sidecar.open(encoding="utf-8") as fh:
            data: object = yaml.full_load(fh)
        assert isinstance(data, dict)
        assert data["origin_time"] == "2024-06-01T00:00:00+00:00"
        assert data["origin_source"] == "/rip/Beethoven"


class TestFindFreeddbSidecar:
    """Tests for :func:`music_annotator._pipeline_io._find_freedb_sidecar`."""

    def test_finds_first_freedb_yaml(self, fs: FakeFilesystem) -> None:
        """Returns the first freedb_disc_N.yaml file found in the work_top_dir.

        :param fs: pyfakefs fixture.
        """
        work_dir = Path("/lib/Composer/Work")
        fs.create_dir(str(work_dir))
        fs.create_file(str(work_dir / "freedb_disc_1.yaml"), contents=b"")
        result = _find_freedb_sidecar(work_dir)
        assert result == work_dir / "freedb_disc_1.yaml"

    def test_returns_none_when_absent(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """Returns None when no freedb_disc_N.yaml file exists.

        :param fs: pyfakefs fixture.
        """
        work_dir = Path("/lib/Composer/Work")
        fs.create_dir(str(work_dir))
        assert _find_freedb_sidecar(work_dir) is None

    def test_returns_first_sorted_when_multiple(self, fs: FakeFilesystem) -> None:
        """Returns the first (sorted) freedb_disc_N.yaml when multiple exist.

        :param fs: pyfakefs fixture.
        """
        work_dir = Path("/lib/Composer/Work")
        fs.create_dir(str(work_dir))
        fs.create_file(str(work_dir / "freedb_disc_2.yaml"), contents=b"")
        fs.create_file(str(work_dir / "freedb_disc_1.yaml"), contents=b"")
        result = _find_freedb_sidecar(work_dir)
        assert result == work_dir / "freedb_disc_1.yaml"


# ---------------------------------------------------------------------------
# enrich_origin_time — full pipeline tests
# ---------------------------------------------------------------------------


def _write_journal(dest_root: Path, entries: list[dict[str, str]]) -> None:
    """Write a journal JSON file to ``dest_root / music_annotator_journal.json``.

    :param dest_root: Destination root directory (must already exist).
    :param entries: List of raw entry dicts to serialise.
    """
    journal_path = dest_root / JOURNAL_FILENAME
    journal_path.write_text(json.dumps(entries), encoding="utf-8")


class TestEnrichOriginTime:
    """Tests for :func:`music_annotator._pipeline_io.enrich_origin_time`.

    Covers: freedb_disc_N.yaml write path; music_annotator_provenance.yaml fallback path;
    idempotency (run twice, same result); empty journal no-op; dry_run mode.
    """

    def test_writes_to_freedb_sidecar(self, fs: FakeFilesystem) -> None:
        """enrich_origin_time writes origin_time and origin_source into an existing freedb_disc_N.yaml.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_top_dir = dest_root / "Beethoven" / "Symphony No 5 [2024]"
        fs.create_dir(str(work_top_dir))
        freedb_sidecar = work_top_dir / "freedb_disc_1.yaml"
        freedb_sidecar.write_text("disc_id: [12345, 2, 150, 300]\n", encoding="utf-8")

        _write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T08:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/rip/Beethoven/01.flac",
                    "destination": str(work_top_dir / "01 - I.flac"),
                    "action": "tagged",
                }
            ],
        )

        enrich_origin_time(dest_root)

        result = _read_provenance_sidecar(freedb_sidecar)
        assert result.origin_time == "2024-06-01T08:00:00+00:00"
        assert result.origin_source == "/rip/Beethoven"

    def test_writes_provenance_yaml_when_no_freedb(self, fs: FakeFilesystem) -> None:
        """enrich_origin_time creates music_annotator_provenance.yaml when no freedb_disc_N.yaml exists.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_top_dir = dest_root / "Presto" / "Album [2024]"
        fs.create_dir(str(work_top_dir))

        _write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-05-15T12:00:00+00:00",
                    "release_id": "rel-2",
                    "source": "/downloads/Presto/01.flac",
                    "destination": str(work_top_dir / "01 - Track.flac"),
                    "action": "tagged",
                }
            ],
        )

        enrich_origin_time(dest_root)

        provenance_path = work_top_dir / PROVENANCE_FILENAME
        assert provenance_path.is_file()
        result = _read_provenance_sidecar(provenance_path)
        assert result.origin_time == "2024-05-15T12:00:00+00:00"
        assert result.origin_source == "/downloads/Presto"

    def test_idempotent_run_twice_same_result(self, fs: FakeFilesystem) -> None:
        """Running enrich_origin_time twice produces the same sidecar content (idempotency).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_top_dir = dest_root / "Beethoven" / "Symphony No 5 [2024]"
        fs.create_dir(str(work_top_dir))
        freedb_sidecar = work_top_dir / "freedb_disc_1.yaml"
        freedb_sidecar.write_text("disc_id: [12345, 2, 150, 300]\n", encoding="utf-8")

        _write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T08:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/rip/Beethoven/01.flac",
                    "destination": str(work_top_dir / "01 - I.flac"),
                    "action": "tagged",
                }
            ],
        )

        enrich_origin_time(dest_root)
        first_content = freedb_sidecar.read_text(encoding="utf-8")

        enrich_origin_time(dest_root)
        second_content = freedb_sidecar.read_text(encoding="utf-8")

        assert first_content == second_content

    def test_empty_journal_is_noop(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich_origin_time is a no-op when the journal has no tagged entries.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))
        _write_journal(dest_root, [])

        mock_log = mocker.patch("music_annotator._pipeline_io.log")
        enrich_origin_time(dest_root)

        nothing_calls = [c for c in mock_log.info.call_args_list if c.args and c.args[0] == "enrich_origin_time_nothing_to_do"]
        assert len(nothing_calls) == 1

    def test_dry_run_no_writes(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich_origin_time(dry_run=True) logs planned writes but does not modify any files.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_top_dir = dest_root / "Beethoven" / "Symphony No 5 [2024]"
        fs.create_dir(str(work_top_dir))
        freedb_sidecar = work_top_dir / "freedb_disc_1.yaml"
        original_content = "disc_id: [12345, 2, 150, 300]\n"
        freedb_sidecar.write_text(original_content, encoding="utf-8")

        _write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T08:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/rip/Beethoven/01.flac",
                    "destination": str(work_top_dir / "01 - I.flac"),
                    "action": "tagged",
                }
            ],
        )

        mock_log = mocker.patch("music_annotator._pipeline_io.log")
        enrich_origin_time(dest_root, dry_run=True)

        # File must not be modified
        assert freedb_sidecar.read_text(encoding="utf-8") == original_content

        # dry_run log event emitted
        dry_run_calls = [c for c in mock_log.info.call_args_list if c.args and c.args[0] == "enrich_origin_time_dry_run"]
        assert len(dry_run_calls) == 1

    def test_earliest_timestamp_selected(self, fs: FakeFilesystem) -> None:
        """The earliest timestamp across all tagged entries for a work_dir is used as origin_time.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_top_dir = dest_root / "Beethoven" / "Symphony No 5 [2024]"
        fs.create_dir(str(work_top_dir))
        provenance_path = work_top_dir / PROVENANCE_FILENAME

        _write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-03T10:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/rip/Beethoven/03.flac",
                    "destination": str(work_top_dir / "03 - III.flac"),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-01T06:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/rip/Beethoven/01.flac",
                    "destination": str(work_top_dir / "01 - I.flac"),
                    "action": "tagged",
                },
                {
                    "timestamp": "2024-06-02T08:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/rip/Beethoven/02.flac",
                    "destination": str(work_top_dir / "02 - II.flac"),
                    "action": "tagged",
                },
            ],
        )

        enrich_origin_time(dest_root)

        result = _read_provenance_sidecar(provenance_path)
        assert result.origin_time == "2024-06-01T06:00:00+00:00"
        assert result.origin_source == "/rip/Beethoven"

    def test_noop_when_fields_already_present(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """enrich_origin_time is a no-op when the sidecar already has the correct provenance fields.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_top_dir = dest_root / "Beethoven" / "Symphony No 5 [2024]"
        fs.create_dir(str(work_top_dir))
        freedb_sidecar = work_top_dir / "freedb_disc_1.yaml"
        freedb_sidecar.write_text(
            "origin_time: '2024-06-01T08:00:00+00:00'\norigin_source: /rip/Beethoven\n",
            encoding="utf-8",
        )

        _write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-06-01T08:00:00+00:00",
                    "release_id": "rel-1",
                    "source": "/rip/Beethoven/01.flac",
                    "destination": str(work_top_dir / "01 - I.flac"),
                    "action": "tagged",
                }
            ],
        )

        original_content = freedb_sidecar.read_text(encoding="utf-8")
        mock_log = mocker.patch("music_annotator._pipeline_io.log")
        enrich_origin_time(dest_root)

        # File must not be modified
        assert freedb_sidecar.read_text(encoding="utf-8") == original_content

        # noop log event emitted at debug level
        noop_calls = [c for c in mock_log.debug.call_args_list if c.args and c.args[0] == "enrich_origin_time_noop"]
        assert len(noop_calls) == 1


# ---------------------------------------------------------------------------
# _mtime_iso
# ---------------------------------------------------------------------------


class TestMtimeIso:
    """Tests for :func:`music_annotator._pipeline_io._mtime_iso`."""

    def test_returns_iso8601_utc_string(self, fs: FakeFilesystem) -> None:
        """_mtime_iso returns a timezone-aware ISO-8601 UTC string for an existing file.

        :param fs: pyfakefs fixture.
        """
        path = Path("/tmp/test.flac")
        fs.create_file(str(path), contents=b"data")
        result = _mtime_iso(path)
        # Must be a non-empty string ending with UTC offset
        assert result
        assert "+" in result or result.endswith("Z")


# ---------------------------------------------------------------------------
# rebuild_journal
# ---------------------------------------------------------------------------


class TestRebuildJournal:
    """Tests for :func:`music_annotator._pipeline_io.rebuild_journal`.

    Covers: dry-run (default) vs write mode; origin-time present/absent; mixed FLAC+MP3;
    missing dest_root; sidecar files included; journal file excluded from entries.
    """

    def test_dry_run_does_not_write_journal(self, fs: FakeFilesystem) -> None:
        """rebuild_journal(dry_run=True) returns entries without replacing the on-disk journal.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))
        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Movement", musicbrainz_albumid="rel-1"))

        original_journal = "[]\n"
        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text(original_journal, encoding="utf-8")

        result = rebuild_journal(dest_root, dry_run=True)

        # Journal file must not be modified
        assert journal_path.read_text(encoding="utf-8") == original_journal
        # Returned log has one audio entry
        assert len(result.entries) == 1
        assert result.entries[0].action == "tagged"
        assert result.entries[0].destination == str(flac_path)
        assert result.entries[0].release_id == "rel-1"

    def test_write_mode_replaces_journal(self, fs: FakeFilesystem) -> None:
        """rebuild_journal(dry_run=False) replaces the on-disk journal with the rebuilt entries.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))
        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Movement", musicbrainz_albumid="rel-1"))

        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("[]", encoding="utf-8")

        result = rebuild_journal(dest_root, dry_run=False)

        # Journal file must be replaced with the rebuilt entries
        written = json.loads(journal_path.read_text(encoding="utf-8"))
        assert len(written) == 1
        assert written[0]["action"] == "tagged"
        assert written[0]["destination"] == str(flac_path)
        assert len(result.entries) == 1

    def test_origin_time_present(self, fs: FakeFilesystem) -> None:
        """rebuild_journal populates origin_time from the freedb_disc_N.yaml sidecar.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))
        freedb_sidecar = work_dir / "freedb_disc_1.yaml"
        freedb_sidecar.write_text(
            "origin_time: '2024-05-01T10:00:00+00:00'\norigin_source: /rip/source\n",
            encoding="utf-8",
        )
        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Movement", musicbrainz_albumid="rel-1"))

        result = rebuild_journal(dest_root, dry_run=True)

        audio_entries = [e for e in result.entries if e.action == "tagged"]
        assert len(audio_entries) == 1
        assert audio_entries[0].origin_time == "2024-05-01T10:00:00+00:00"

    def test_origin_time_absent(self, fs: FakeFilesystem) -> None:
        """rebuild_journal sets origin_time to empty string when no provenance sidecar exists.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))
        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Movement", musicbrainz_albumid="rel-1"))

        result = rebuild_journal(dest_root, dry_run=True)

        audio_entries = [e for e in result.entries if e.action == "tagged"]
        assert len(audio_entries) == 1
        assert audio_entries[0].origin_time == ""

    def test_mixed_flac_and_mp3(self, fs: FakeFilesystem) -> None:
        """rebuild_journal reconstructs entries for both FLAC and MP3 files.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))

        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Movement 1", musicbrainz_albumid="rel-1"))

        mp3_path = work_dir / "02 - Movement.mp3"
        fs.create_file(str(mp3_path), contents=_MINIMAL_MP3)
        apply_tags_mp3(mp3_path, TrackTags(title="Movement 2", musicbrainz_albumid="rel-1"))

        result = rebuild_journal(dest_root, dry_run=True)

        audio_entries = [e for e in result.entries if e.action == "tagged"]
        assert len(audio_entries) == 2
        destinations = {e.destination for e in audio_entries}
        assert str(flac_path) in destinations
        assert str(mp3_path) in destinations
        for entry in audio_entries:
            assert entry.release_id == "rel-1"

    def test_sidecar_files_included(self, fs: FakeFilesystem) -> None:
        """rebuild_journal includes sidecar YAML and image files as action="sidecar" entries.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))

        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Movement", musicbrainz_albumid="rel-1"))

        cover_path = work_dir / "cover.jpg"
        fs.create_file(str(cover_path), contents=b"\xff\xd8\xff\xe0")

        freedb_path = work_dir / "freedb_disc_1.yaml"
        fs.create_file(str(freedb_path), contents=b"disc_id: [1, 2, 3]\n")

        result = rebuild_journal(dest_root, dry_run=True)

        sidecar_entries = [e for e in result.entries if e.action == "sidecar"]
        sidecar_dests = {e.destination for e in sidecar_entries}
        assert str(cover_path) in sidecar_dests
        assert str(freedb_path) in sidecar_dests
        for entry in sidecar_entries:
            assert entry.release_id == ""

    def test_journal_file_excluded(self, fs: FakeFilesystem) -> None:
        """rebuild_journal never includes the journal file itself as an entry.

        A music_annotator_journal.json file inside a work_dir (unusual but possible) must be
        skipped by the _REBUILD_SKIP_FILENAMES guard.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))
        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Movement", musicbrainz_albumid="rel-1"))

        # Place a journal file inside the work_dir to exercise the skip-filename guard
        inner_journal = work_dir / JOURNAL_FILENAME
        inner_journal.write_text("[]", encoding="utf-8")

        # Also place the real journal at dest_root level
        journal_path = dest_root / JOURNAL_FILENAME
        journal_path.write_text("[]", encoding="utf-8")

        result = rebuild_journal(dest_root, dry_run=True)

        all_dests = {e.destination for e in result.entries}
        assert str(inner_journal) not in all_dests
        assert str(journal_path) not in all_dests

    def test_missing_dest_root_returns_empty(self, fs: FakeFilesystem) -> None:  # pylint: disable=unused-argument
        """rebuild_journal returns an empty TransactionLog when dest_root does not exist.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/nonexistent")
        result = rebuild_journal(dest_root, dry_run=True)
        assert result.entries == []

    def test_audio_hash_recomputed(self, fs: FakeFilesystem) -> None:
        """rebuild_journal recomputes audio_hash from the file's decoded audio content.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))
        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Movement", musicbrainz_albumid="rel-1"))

        result = rebuild_journal(dest_root, dry_run=True)

        audio_entries = [e for e in result.entries if e.action == "tagged"]
        assert len(audio_entries) == 1
        # audio_hash must be a non-empty algorithm-tagged string for FLAC
        assert audio_entries[0].audio_hash.startswith("flac-md5:")

    def test_timestamp_from_mtime(self, fs: FakeFilesystem) -> None:
        """rebuild_journal derives timestamp from the file's mtime as an ISO-8601 UTC string.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))
        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Movement", musicbrainz_albumid="rel-1"))

        result = rebuild_journal(dest_root, dry_run=True)

        audio_entries = [e for e in result.entries if e.action == "tagged"]
        assert len(audio_entries) == 1
        ts = audio_entries[0].timestamp
        assert ts  # non-empty
        # ISO-8601 UTC: must contain a "+" or end with "Z"
        assert "+" in ts or ts.endswith("Z")

    def test_non_dir_under_top_dir_skipped(self, fs: FakeFilesystem) -> None:
        """rebuild_journal skips non-directory entries directly under a top_dir.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        top_dir = dest_root / "Composer"
        fs.create_dir(str(top_dir))
        # A file directly under top_dir (not a work_dir) must be skipped
        stray_file = top_dir / "stray.txt"
        fs.create_file(str(stray_file), contents=b"stray")

        result = rebuild_journal(dest_root, dry_run=True)

        assert result.entries == []

    def test_subdir_in_work_dir_not_added_as_entry(self, fs: FakeFilesystem) -> None:
        """rebuild_journal skips subdirectories encountered during rglob (only files are entries).

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        sub_dir = work_dir / "Act I"
        fs.create_dir(str(sub_dir))
        flac_path = sub_dir / "01 - Scene.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Scene", musicbrainz_albumid="rel-1"))

        result = rebuild_journal(dest_root, dry_run=True)

        # Only the FLAC file should appear; the subdirectory itself must not be an entry
        assert all(e.destination != str(sub_dir) for e in result.entries)
        audio_entries = [e for e in result.entries if e.action == "tagged"]
        assert len(audio_entries) == 1

    def test_unknown_extension_file_skipped(self, fs: FakeFilesystem) -> None:
        """rebuild_journal silently skips files with extensions that are neither audio nor sidecar.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))
        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Movement", musicbrainz_albumid="rel-1"))
        # A .cue file should be silently skipped
        cue_path = work_dir / "disc.cue"
        fs.create_file(str(cue_path), contents=b"FILE disc.wav WAVE\n")

        result = rebuild_journal(dest_root, dry_run=True)

        all_dests = {e.destination for e in result.entries}
        assert str(cue_path) not in all_dests
        assert len([e for e in result.entries if e.action == "tagged"]) == 1

    def test_read_albumid_from_tags_exception_returns_empty(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """_read_albumid_from_tags returns empty string when tag read raises an exception.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        work_dir = dest_root / "Composer" / "Work [2024]"
        fs.create_dir(str(work_dir))
        flac_path = work_dir / "01 - Movement.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)

        mocker.patch("music_annotator._pipeline_io._read_tags_flac", side_effect=RuntimeError("corrupt"))
        result = _read_albumid_from_tags(flac_path)
        assert result == ""

    def test_class_prefixed_path_rebuilt_correctly(self, fs: FakeFilesystem) -> None:
        """rebuild_journal handles class-prefixed three-level paths (C-CLASS).

        A library with a class-prefixed path (``dest_root/Classical/Composer/Work/leaf.flac``)
        must be walked correctly: the class directory is detected as a known C-CLASS name and
        the walk iterates one level deeper to find the actual work directories.  Also exercises
        the non-dir-in-artist-dir, non-dir-in-work-dir, skip-filename, and sidecar-file branches
        of the class-prefixed walk.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        # Class-prefixed three-level path: dest_root / Classical / <top_dir> / <work_dir> / leaf
        work_dir = dest_root / "Classical" / "Beethoven - Karajan" / "Symphony No. 9 [rec 1962]"
        fs.create_dir(str(work_dir))
        flac_path = work_dir / "01 - Allegro.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        apply_tags_flac(flac_path, TrackTags(title="Allegro", musicbrainz_albumid="rel-class-1"))

        # A sidecar file in the work_dir (exercises the sidecar branch).
        cover_path = work_dir / "cover.jpg"
        fs.create_file(str(cover_path), contents=b"\xff\xd8\xff\xe0")

        # A skip-filename file in the work_dir (exercises the skip-filename branch).
        journal_in_work = work_dir / JOURNAL_FILENAME
        journal_in_work.write_text("[]", encoding="utf-8")

        # A non-dir file directly under the artist dir (exercises the non-dir-in-artist-dir branch).
        artist_dir = dest_root / "Classical" / "Beethoven - Karajan"
        stray_in_artist = artist_dir / "stray.txt"
        fs.create_file(str(stray_in_artist), contents=b"stray")

        # A non-dir file directly under the class dir (exercises the non-dir-in-class-dir branch).
        stray_in_class = dest_root / "Classical" / "stray.txt"
        fs.create_file(str(stray_in_class), contents=b"stray")

        # A subdirectory inside the work_dir (exercises the non-file branch in rglob).
        sub_dir = work_dir / "Act I"
        fs.create_dir(str(sub_dir))

        # A .cue file (unknown extension — neither audio nor sidecar) exercises the elif-False branch.
        cue_path = work_dir / "disc.cue"
        fs.create_file(str(cue_path), contents=b"FILE disc.wav WAVE\n")

        result = rebuild_journal(dest_root, dry_run=True)

        audio_entries = [e for e in result.entries if e.action == "tagged"]
        assert len(audio_entries) == 1, f"Expected 1 audio entry, got {len(audio_entries)}"
        assert audio_entries[0].destination == str(flac_path)
        assert audio_entries[0].release_id == "rel-class-1"

        sidecar_entries = [e for e in result.entries if e.action == "sidecar"]
        sidecar_dests = {e.destination for e in sidecar_entries}
        assert str(cover_path) in sidecar_dests, "cover.jpg must be included as a sidecar entry"
        assert str(journal_in_work) not in sidecar_dests, "journal file must be skipped"


# ---------------------------------------------------------------------------
# Annotation-tier vocabulary round-trip tests (C-TIER KAT)
# ---------------------------------------------------------------------------


class TestAnnotationTierVocabularyRoundtrips:
    """KAT: write each tier + needs_spot_check to a sidecar, read back, assert equality.

    Covers the C-TIER contract: all five AnnotationTier values persist correctly through
    _write_provenance_fields / _read_provenance_sidecar, and the monotonic-upgrade rule
    prevents lowering a tier.
    """

    def test_all_tiers_roundtrip(self, fs: FakeFilesystem) -> None:
        """Each AnnotationTier value survives a write/read round-trip through the YAML sidecar.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/music_annotator_provenance.yaml")
        fs.create_dir(str(sidecar.parent))

        for tier in AnnotationTier:
            # Reset sidecar for each tier
            if sidecar.exists():
                sidecar.unlink()
            provenance = ProvenanceSidecar(
                origin_time="2024-06-01T00:00:00+00:00",
                origin_source="/rip/source",
                annotation_tier=tier,
                needs_spot_check=(tier == AnnotationTier.MB_SEARCH_RESOLVED),
            )
            _write_provenance_fields(sidecar, provenance)
            result = _read_provenance_sidecar(sidecar)
            assert result.annotation_tier == tier, f"round-trip failed for tier {tier!r}"
            expected_spot_check = tier == AnnotationTier.MB_SEARCH_RESOLVED
            assert result.needs_spot_check == expected_spot_check, f"needs_spot_check mismatch for tier {tier!r}"

    def test_needs_spot_check_true_for_search_resolved(self, fs: FakeFilesystem) -> None:
        """needs_spot_check=True is persisted and read back correctly for mb-search-resolved.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/music_annotator_provenance.yaml")
        fs.create_dir(str(sidecar.parent))
        provenance = ProvenanceSidecar(
            origin_time="2024-06-01T00:00:00+00:00",
            origin_source="/rip/source",
            annotation_tier=AnnotationTier.MB_SEARCH_RESOLVED,
            needs_spot_check=True,
        )
        _write_provenance_fields(sidecar, provenance)
        result = _read_provenance_sidecar(sidecar)
        assert result.annotation_tier == AnnotationTier.MB_SEARCH_RESOLVED
        assert result.needs_spot_check is True

    def test_needs_spot_check_false_for_full_mb_verified(self, fs: FakeFilesystem) -> None:
        """needs_spot_check=False is persisted and read back correctly for full-mb-verified.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/music_annotator_provenance.yaml")
        fs.create_dir(str(sidecar.parent))
        provenance = ProvenanceSidecar(
            origin_time="2024-06-01T00:00:00+00:00",
            origin_source="/rip/source",
            annotation_tier=AnnotationTier.FULL_MB_VERIFIED,
            needs_spot_check=False,
        )
        _write_provenance_fields(sidecar, provenance)
        result = _read_provenance_sidecar(sidecar)
        assert result.annotation_tier == AnnotationTier.FULL_MB_VERIFIED
        assert result.needs_spot_check is False

    def test_monotonic_upgrade_raises_tier(self, fs: FakeFilesystem) -> None:
        """annotation_tier is overwritten when the incoming tier ranks strictly higher.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/music_annotator_provenance.yaml")
        fs.create_dir(str(sidecar.parent))

        # Write a low tier first
        low = ProvenanceSidecar(
            origin_time="2024-06-01T00:00:00+00:00",
            origin_source="/rip/source",
            annotation_tier=AnnotationTier.SOURCE_TAGS_ONLY,
            needs_spot_check=False,
        )
        _write_provenance_fields(sidecar, low)

        # Now write a higher tier — must overwrite
        high = ProvenanceSidecar(
            origin_time="2024-06-01T00:00:00+00:00",
            origin_source="/rip/source",
            annotation_tier=AnnotationTier.MB_SEARCH_RESOLVED,
            needs_spot_check=True,
        )
        _write_provenance_fields(sidecar, high)

        result = _read_provenance_sidecar(sidecar)
        assert result.annotation_tier == AnnotationTier.MB_SEARCH_RESOLVED
        assert result.needs_spot_check is True

    def test_monotonic_upgrade_does_not_lower_tier(self, fs: FakeFilesystem) -> None:
        """annotation_tier is NOT overwritten when the incoming tier ranks lower or equal.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/music_annotator_provenance.yaml")
        fs.create_dir(str(sidecar.parent))

        # Write a high tier first
        high = ProvenanceSidecar(
            origin_time="2024-06-01T00:00:00+00:00",
            origin_source="/rip/source",
            annotation_tier=AnnotationTier.FULL_MB_VERIFIED,
            needs_spot_check=False,
        )
        _write_provenance_fields(sidecar, high)

        # Attempt to lower the tier — must be rejected
        low = ProvenanceSidecar(
            origin_time="2024-06-01T00:00:00+00:00",
            origin_source="/rip/source",
            annotation_tier=AnnotationTier.SOURCE_TAGS_ONLY,
            needs_spot_check=False,
        )
        _write_provenance_fields(sidecar, low)

        result = _read_provenance_sidecar(sidecar)
        # Tier must remain at the higher value
        assert result.annotation_tier == AnnotationTier.FULL_MB_VERIFIED

    def test_empty_incoming_tier_not_written(self, fs: FakeFilesystem) -> None:
        """An empty annotation_tier on the incoming provenance is not written to the sidecar.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/music_annotator_provenance.yaml")
        fs.create_dir(str(sidecar.parent))

        # Write a tier first
        provenance = ProvenanceSidecar(
            origin_time="2024-06-01T00:00:00+00:00",
            origin_source="/rip/source",
            annotation_tier=AnnotationTier.MB_PARTIAL,
            needs_spot_check=False,
        )
        _write_provenance_fields(sidecar, provenance)

        # Now write with empty tier — must not overwrite
        empty_tier = ProvenanceSidecar(
            origin_time="2024-06-01T00:00:00+00:00",
            origin_source="/rip/source",
            annotation_tier="",
            needs_spot_check=False,
        )
        _write_provenance_fields(sidecar, empty_tier)

        result = _read_provenance_sidecar(sidecar)
        assert result.annotation_tier == AnnotationTier.MB_PARTIAL

    def test_annotation_tier_rank_ordering(self) -> None:
        """annotation_tier_rank returns strictly increasing values from lowest to highest tier.

        Verifies the ordering: source-tags-only < alternate-source < mb-partial
        < mb-search-resolved < full-mb-verified.
        """
        ordered = [
            AnnotationTier.SOURCE_TAGS_ONLY,
            AnnotationTier.ALTERNATE_SOURCE,
            AnnotationTier.MB_PARTIAL,
            AnnotationTier.MB_SEARCH_RESOLVED,
            AnnotationTier.FULL_MB_VERIFIED,
        ]
        ranks = [annotation_tier_rank(t) for t in ordered]
        assert ranks == sorted(ranks), "tier ranks must be strictly increasing"
        assert len(set(ranks)) == len(ranks), "all tier ranks must be distinct"

    def test_invalid_current_tier_in_sidecar_allows_write(self, fs: FakeFilesystem) -> None:
        """When the sidecar contains an unrecognised tier string, the incoming tier is written.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/music_annotator_provenance.yaml")
        fs.create_dir(str(sidecar.parent))
        # Write a sidecar with a bogus tier value
        sidecar.write_text(
            "origin_time: '2024-06-01T00:00:00+00:00'\norigin_source: /rip/source\nannotation_tier: not-a-valid-tier\n",
            encoding="utf-8",
        )

        provenance = ProvenanceSidecar(
            origin_time="2024-06-01T00:00:00+00:00",
            origin_source="/rip/source",
            annotation_tier=AnnotationTier.MB_PARTIAL,
            needs_spot_check=False,
        )
        _write_provenance_fields(sidecar, provenance)

        result = _read_provenance_sidecar(sidecar)
        assert result.annotation_tier == AnnotationTier.MB_PARTIAL

    def test_unrecognised_incoming_tier_string_not_written(self, fs: FakeFilesystem) -> None:
        """An unrecognised annotation_tier string on the incoming provenance is not written.

        This exercises the ValueError branch in _write_provenance_fields where the incoming
        tier string cannot be coerced to a valid AnnotationTier value.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/music_annotator_provenance.yaml")
        fs.create_dir(str(sidecar.parent))
        # Write a valid tier first
        provenance = ProvenanceSidecar(
            origin_time="2024-06-01T00:00:00+00:00",
            origin_source="/rip/source",
            annotation_tier=AnnotationTier.MB_PARTIAL,
            needs_spot_check=False,
        )
        _write_provenance_fields(sidecar, provenance)

        # Pass a ProvenanceSidecar with an unrecognised tier string (bypassing Pydantic validation).
        # model_construct skips validation so annotation_tier stays as a plain unrecognised string.
        bad_provenance = ProvenanceSidecar.model_construct(
            origin_time="2024-06-01T00:00:00+00:00",
            origin_source="/rip/source",
            annotation_tier="not-a-valid-tier",
            needs_spot_check=False,
        )
        _write_provenance_fields(sidecar, bad_provenance)

        # The existing tier must remain unchanged because the incoming value was unrecognised
        result = _read_provenance_sidecar(sidecar)
        assert result.annotation_tier == AnnotationTier.MB_PARTIAL


# ---------------------------------------------------------------------------
# applied_case_ids set-union merge (C-CASE-PROV KAT)
# ---------------------------------------------------------------------------


class TestAppliedCaseIdsMerge:
    """KAT: set-union append-only merge for applied_case_ids in _write_provenance_fields.

    Covers the C-CASE-PROV merge contract:
    (a) writing a non-empty set to an empty sidecar records the sorted set;
    (b) a second write with a disjoint set yields the sorted union (append-only proof);
    (c) a write with an empty list leaves the recorded set unchanged (empty-never-erases proof);
    (d) a ProvenanceSidecar round-trips through _read_provenance_sidecar carrying the case-IDs.
    """

    def test_initial_write_records_sorted_set(self, fs: FakeFilesystem) -> None:
        """Writing applied_case_ids to an empty sidecar records the sorted set.

        Covers the superset-incoming (write) branch of the merge arm.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/music_annotator_provenance.yaml")
        fs.create_dir(str(sidecar.parent))

        provenance = ProvenanceSidecar(
            origin_time="2024-06-01T00:00:00+00:00",
            origin_source="/rip/Beethoven",
            applied_case_ids=["SEL-11", "REND-14"],
        )
        _write_provenance_fields(sidecar, provenance)

        result = _read_provenance_sidecar(sidecar)
        # Sorted order: REND-14 < SEL-11
        assert result.applied_case_ids == ["REND-14", "SEL-11"]

    def test_second_write_yields_union(self, fs: FakeFilesystem) -> None:
        """A second write with a disjoint set yields the sorted union (append-only proof).

        Covers the superset-incoming (write) branch a second time, confirming that previously
        recorded case-IDs are never retracted.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/music_annotator_provenance.yaml")
        fs.create_dir(str(sidecar.parent))

        # First write: two case-IDs
        _write_provenance_fields(
            sidecar,
            ProvenanceSidecar(
                origin_time="2024-06-01T00:00:00+00:00",
                origin_source="/rip/Beethoven",
                applied_case_ids=["SEL-11", "REND-14"],
            ),
        )

        # Second write: one new case-ID, no overlap with the first set
        _write_provenance_fields(
            sidecar,
            ProvenanceSidecar(
                origin_time="2024-06-01T00:00:00+00:00",
                origin_source="/rip/Beethoven",
                applied_case_ids=["NORM-2"],
            ),
        )

        result = _read_provenance_sidecar(sidecar)
        # Union of {"SEL-11","REND-14"} and {"NORM-2"}, sorted
        assert result.applied_case_ids == ["NORM-2", "REND-14", "SEL-11"]

    def test_empty_incoming_never_erases_recorded_set(self, fs: FakeFilesystem) -> None:
        """A write with an empty applied_case_ids list leaves the recorded set unchanged.

        Covers the empty-incoming branch: an incoming empty list must never shrink or erase
        the recorded set.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/music_annotator_provenance.yaml")
        fs.create_dir(str(sidecar.parent))

        # Establish a recorded set
        _write_provenance_fields(
            sidecar,
            ProvenanceSidecar(
                origin_time="2024-06-01T00:00:00+00:00",
                origin_source="/rip/Beethoven",
                applied_case_ids=["NORM-2", "REND-14", "SEL-11"],
            ),
        )

        # Write with empty list — must not erase the recorded set
        _write_provenance_fields(
            sidecar,
            ProvenanceSidecar(
                origin_time="2024-06-01T00:00:00+00:00",
                origin_source="/rip/Beethoven",
                applied_case_ids=[],
            ),
        )

        result = _read_provenance_sidecar(sidecar)
        assert result.applied_case_ids == ["NORM-2", "REND-14", "SEL-11"]

    def test_round_trip_preserves_case_ids(self, fs: FakeFilesystem) -> None:
        """A ProvenanceSidecar round-trips through _read_provenance_sidecar carrying the case-IDs.

        Covers the subset-incoming (no-change) branch: writing the same set a second time must
        leave the file content unchanged (idempotency), and the read-back value must equal the
        original.

        :param fs: pyfakefs fixture.
        """
        sidecar = Path("/lib/Composer/Work/music_annotator_provenance.yaml")
        fs.create_dir(str(sidecar.parent))

        provenance = ProvenanceSidecar(
            origin_time="2024-06-01T00:00:00+00:00",
            origin_source="/rip/Beethoven",
            annotation_tier=AnnotationTier.FULL_MB_VERIFIED,
            applied_case_ids=["NORM-2", "REND-14", "SEL-11"],
        )
        _write_provenance_fields(sidecar, provenance)

        # Second write with the same set — no-change branch; file content must be stable
        _write_provenance_fields(sidecar, provenance)

        result = _read_provenance_sidecar(sidecar)
        assert result.applied_case_ids == ["NORM-2", "REND-14", "SEL-11"]
        assert result.annotation_tier == AnnotationTier.FULL_MB_VERIFIED


# ---------------------------------------------------------------------------
# Annotation-tier write path (_copy_tag_verify_journal_pass + run())
# ---------------------------------------------------------------------------


class TestAnnotationTierWritePath:
    """Tests for the annotation-tier write path wired into the ingest pipeline.

    Covers:
    - _copy_tag_verify_journal_pass writes annotation_tier to PROVENANCE_FILENAME when no
      freedb sidecar exists (the ``if _sidecar_path is None`` True branch).
    - _copy_tag_verify_journal_pass writes annotation_tier to the freedb sidecar when one
      exists (the ``if _sidecar_path is None`` False branch).
    - run() classifies SEARCH_HIT when source files carry no embedded recording MBIDs.
    - run() classifies EMBEDDED_MBID when source files carry embedded recording MBIDs that
      match the selected medium's track list.
    """

    def _patch_mb_for_run(self, mocker: MockerFixture, release: MBRelease) -> None:
        """Patch all MB API calls and internal helpers used by run().

        :param mocker: pytest-mock fixture.
        :param release: MBRelease model to return from fetch_release.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        mocker.patch("music_annotator._pipeline.fetch_recording_detail", return_value=MBRecording())
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

    def test_tier_written_to_provenance_yaml_when_no_freedb_sidecar(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """annotation_tier is written to PROVENANCE_FILENAME when no freedb_disc_N.yaml exists.

        Exercises the ``if _sidecar_path is None`` True branch in _copy_tag_verify_journal_pass:
        _find_freedb_sidecar returns None, so the tier is written to music_annotator_provenance.yaml.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=1)
        self._patch_mb_for_run(mocker, release)

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )

        flac_files = list(dest.rglob("*.flac"))
        assert flac_files
        work_top = _work_top_dir(Path(flac_files[0]), dest)
        prov_path = work_top / PROVENANCE_FILENAME
        assert prov_path.exists(), "music_annotator_provenance.yaml must be written when no freedb sidecar exists"
        result = _read_provenance_sidecar(prov_path)
        # No embedded MBIDs in source → SEARCH_HIT → mb-search-resolved
        assert result.annotation_tier == AnnotationTier.MB_SEARCH_RESOLVED
        assert result.needs_spot_check is True

    def test_tier_written_to_freedb_sidecar_when_present(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """annotation_tier is written to freedb_disc_N.yaml when it exists in the work directory.

        Exercises the ``if _sidecar_path is None`` False branch in _copy_tag_verify_journal_pass:
        _find_freedb_sidecar returns the freedb path, so the tier is merged into that file.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        yaml_content = b"disc_id: [123, 2, 182, 50000, 3600]\nrecord: []\n"
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / "00 - disc info.yaml"), contents=yaml_content)

        release = _make_release(n_tracks=1)
        self._patch_mb_for_run(mocker, release)

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )

        flac_files = list(dest.rglob("*.flac"))
        assert flac_files
        work_top = _work_top_dir(Path(flac_files[0]), dest)
        freedb_path = work_top / "freedb_disc_1.yaml"
        assert freedb_path.exists(), "freedb_disc_1.yaml must exist (written by _write_freedb_yaml)"
        result = _read_provenance_sidecar(freedb_path)
        # No embedded MBIDs in source → SEARCH_HIT → mb-search-resolved
        assert result.annotation_tier == AnnotationTier.MB_SEARCH_RESOLVED
        assert result.needs_spot_check is True
        # PROVENANCE_FILENAME must NOT be created when freedb sidecar exists
        assert not (work_top / PROVENANCE_FILENAME).exists()

    def test_run_classifies_search_hit_when_no_embedded_mbids(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """run() assigns mb-search-resolved when source files carry no embedded recording MBIDs.

        Source files with no MUSICBRAINZ_TRACKID tag → CensusSignal.SEARCH_HIT →
        AnnotationTier.MB_SEARCH_RESOLVED + needs_spot_check=True.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        # Plain FLAC with no embedded tags
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=1)
        self._patch_mb_for_run(mocker, release)

        log_events: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._pipeline.log.info",
            side_effect=lambda event, **kw: log_events.append({"event": event, **kw}),
        )

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )

        tier_events = [e for e in log_events if e["event"] == "annotation_tier_signal"]
        assert tier_events, "annotation_tier_signal must be logged"
        assert tier_events[0]["signal"] == CensusSignal.SEARCH_HIT

    def test_run_classifies_embedded_mbid_when_source_has_matching_recording_id(
        self, mocker: MockerFixture, fs: FakeFilesystem
    ) -> None:
        """run() assigns full-mb-verified when source files carry embedded recording MBIDs.

        A source FLAC with MUSICBRAINZ_TRACKID matching the release's recording ID →
        CensusSignal.EMBEDDED_MBID → AnnotationTier.FULL_MB_VERIFIED + needs_spot_check=False.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))

        # Write a FLAC with an embedded MUSICBRAINZ_TRACKID matching the release's recording ID.
        flac_path = src / "01.flac"
        fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
        audio = FLAC(str(flac_path))
        audio["musicbrainz_trackid"] = ["rec-1"]  # matches _make_release(n_tracks=1) recording id
        audio.save()

        release = _make_release(n_tracks=1)
        self._patch_mb_for_run(mocker, release)

        log_events: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._pipeline.log.info",
            side_effect=lambda event, **kw: log_events.append({"event": event, **kw}),
        )

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )

        tier_events = [e for e in log_events if e["event"] == "annotation_tier_signal"]
        assert tier_events, "annotation_tier_signal must be logged"
        assert tier_events[0]["signal"] == CensusSignal.EMBEDDED_MBID

        # Verify the sidecar carries full-mb-verified
        flac_files = list(dest.rglob("*.flac"))
        assert flac_files
        work_top = _work_top_dir(Path(flac_files[0]), dest)
        prov_path = work_top / PROVENANCE_FILENAME
        result = _read_provenance_sidecar(prov_path)
        assert result.annotation_tier == AnnotationTier.FULL_MB_VERIFIED
        assert result.needs_spot_check is False


# ---------------------------------------------------------------------------
# Single-disc TOC → full-mb-verified KATs (C-WHIP)
# ---------------------------------------------------------------------------

#: CD frame offsets for a fictional single-disc release (4 tracks).
_SINGLE_DISC_OFFSETS: list[int] = [182, 45000, 90000, 135000]

#: Minimal valid disc info YAML for the single-disc fixture above.
_SINGLE_DISC_YAML: str = (
    "disc_id: [777777777, 4, 182, 45000, 90000, 135000, 3600]\n"
    "record:\n"
    "- disc_info: {category: classical, disc_id: 'aabbccdd', title: 'Composer / Symphony 1'}\n"
    "  preferred: true\n"
    "  track_info: {DTITLE: 'Composer / Symphony 1', DISCID: 'aabbccdd'}\n"
)


def _make_release_with_toc(n_tracks: int = 4, disc_offsets: list[int] | None = None) -> MBRelease:
    """Build a minimal single-disc release model with optional TOC disc entries.

    :param n_tracks: Number of tracks on the single medium.
    :param disc_offsets: Per-track CD frame offsets for the disc entry.  When ``None``, no disc
        entries are added (empty disc_list).
    :returns: An :class:`~music_annotator.models.MBRelease` instance with one medium.
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
    medium: dict[str, JSON] = {"position": 1, "format": "CD", "track-list": tracks}
    if disc_offsets is not None:
        disc_entries: list[dict[str, object]] = [{"offset-list": disc_offsets, "sectors": str(disc_offsets[-1] + 1000)}]
        medium["disc-list"] = disc_entries  # type: ignore[assignment]
    return MBRelease.model_validate(
        {
            "id": "rel-single",
            "title": "Single Disc Album",
            "date": "2001",
            "status": "Official",
            "barcode": "",
            "artist-credit": [
                {
                    "name": "Composer B",
                    "artist": {"id": "a2", "name": "Composer B", "sort-name": "B, Composer"},
                }
            ],
            "release-group": {"id": "rg-2", "primary-type": "Album", "first-release-date": "2001"},
            "label-info-list": [],
            "text-representation": {"script": "Latn", "language": "eng"},
            "medium-list": [medium],
        }
    )


class TestSingleDiscTocPromotion:
    """KATs: single-disc TOC disc-ID → full-mb-verified when whipper provenance is present.

    Pins the C-WHIP trust anchor: ``origin_source == "whipper"`` is required for the single-disc
    TOC promotion.  A bare non-whipper single-disc TOC match keeps the conservative
    ``mb-search-resolved`` tier.  Multi-disc TOC promotion is unchanged.
    """

    def _patch_mb_for_run(self, mocker: MockerFixture, release: MBRelease) -> None:
        """Patch all MB API calls and internal helpers used by run().

        :param mocker: pytest-mock fixture.
        :param release: MBRelease model to return from fetch_release.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        mocker.patch("music_annotator._pipeline.fetch_recording_detail", return_value=MBRecording())
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

    def test_single_disc_toc_yields_full_verified(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Single-disc TOC match + whipper provenance → full-mb-verified, needs_spot_check=False.

        A single-medium release whose 00 - disc info.yaml TOC offsets resolve against the medium's
        disc entries, with origin_source="whipper", and source FLACs carrying NO embedded MBID,
        must yield CensusSignal.EMBEDDED_MBID → AnnotationTier.FULL_MB_VERIFIED + needs_spot_check=False.
        This was previously mb-search-resolved + True (the conservative default for single-disc).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/whipper-rip")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 5):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / _DISC_INFO_FILENAME), contents=_SINGLE_DISC_YAML)

        release = _make_release_with_toc(n_tracks=4, disc_offsets=_SINGLE_DISC_OFFSETS)
        self._patch_mb_for_run(mocker, release)

        log_events: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._pipeline.log.info",
            side_effect=lambda event, **kw: log_events.append({"event": event, **kw}),
        )

        music_annotator.run(
            release_id="rel-single",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
            origin_source="whipper",
        )

        tier_events = [e for e in log_events if e["event"] == "annotation_tier_signal"]
        assert tier_events, "annotation_tier_signal must be logged"
        assert tier_events[0]["signal"] == CensusSignal.EMBEDDED_MBID

        # The disc info YAML causes a freedb sidecar to be written; the tier lands there.
        flac_files = list(dest.rglob("*.flac"))
        assert flac_files
        work_top = _work_top_dir(Path(flac_files[0]), dest)
        sidecar_path = _find_freedb_sidecar(work_top)
        assert sidecar_path is not None, "freedb sidecar must exist (disc info YAML was present)"
        result = _read_provenance_sidecar(sidecar_path)
        assert result.annotation_tier == AnnotationTier.FULL_MB_VERIFIED
        assert result.needs_spot_check is False

    def test_single_disc_toc_no_whipper_stays_search_resolved(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Single-disc TOC match without whipper provenance stays at mb-search-resolved.

        The same single-medium release with a resolving TOC but no whipper origin_source must NOT
        be promoted — the conservative tier (mb-search-resolved, needs_spot_check=True) is kept.
        This pins the C-WHIP trust anchor: the promotion is whipper-anchored, not a blanket loosening.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/non-whipper-rip")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 5):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)
        fs.create_file(str(src / _DISC_INFO_FILENAME), contents=_SINGLE_DISC_YAML)

        release = _make_release_with_toc(n_tracks=4, disc_offsets=_SINGLE_DISC_OFFSETS)
        self._patch_mb_for_run(mocker, release)

        log_events: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._pipeline.log.info",
            side_effect=lambda event, **kw: log_events.append({"event": event, **kw}),
        )

        music_annotator.run(
            release_id="rel-single",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
            # origin_source not set (defaults to "") — no whipper provenance
        )

        tier_events = [e for e in log_events if e["event"] == "annotation_tier_signal"]
        assert tier_events, "annotation_tier_signal must be logged"
        assert tier_events[0]["signal"] == CensusSignal.SEARCH_HIT

        # The disc info YAML causes a freedb sidecar to be written; the tier lands there.
        flac_files = list(dest.rglob("*.flac"))
        assert flac_files
        work_top = _work_top_dir(Path(flac_files[0]), dest)
        sidecar_path = _find_freedb_sidecar(work_top)
        assert sidecar_path is not None, "freedb sidecar must exist (disc info YAML was present)"
        result = _read_provenance_sidecar(sidecar_path)
        assert result.annotation_tier == AnnotationTier.MB_SEARCH_RESOLVED
        assert result.needs_spot_check is True


# ---------------------------------------------------------------------------
# ISRC-match tier promotion KATs (C-ISRC)
# ---------------------------------------------------------------------------


def _make_release_with_isrcs(isrcs_per_track: list[list[str]]) -> MBRelease:
    """Build a minimal single-medium release whose recordings carry ISRC lists.

    Each element of ``isrcs_per_track`` is the ``isrc_list`` for the corresponding track's
    recording.  An empty inner list means the recording has no ISRCs (``isrc_list == []``).

    :param isrcs_per_track: Per-track ISRC lists; length determines the track count.
    :returns: An :class:`~music_annotator.models.MBRelease` instance with one medium.
    """
    tracks: list[JSON] = []
    for i, isrcs in enumerate(isrcs_per_track, start=1):
        isrc_json: list[JSON] = list(isrcs)
        recording: dict[str, JSON] = {
            "id": f"rec-isrc-{i}",
            "title": f"ISRC Track {i}",
            "artist-credit": [],
            "isrc-list": isrc_json,
        }
        tracks.append({"id": f"trk-isrc-{i}", "position": i, "recording": recording})
    return MBRelease.model_validate(
        {
            "id": "rel-isrc",
            "title": "ISRC Album",
            "date": "2020",
            "status": "Official",
            "barcode": "",
            "artist-credit": [
                {
                    "name": "Composer ISRC",
                    "artist": {"id": "a-isrc", "name": "Composer ISRC", "sort-name": "ISRC, Composer"},
                }
            ],
            "release-group": {"id": "rg-isrc", "primary-type": "Album", "first-release-date": "2020"},
            "label-info-list": [],
            "text-representation": {"script": "Latn", "language": "eng"},
            "medium-list": [{"position": 1, "format": "CD", "track-list": tracks}],
        }
    )


class TestIsrcMatchTierPromotion:
    """KATs for C-ISRC: ISRC-match rung in run()'s signal ladder.

    Covers:
    - All source ISRCs match → CensusSignal.ISRC_MATCH → full-mb-verified, needs_spot_check=False.
    - One source ISRC mismatches → SEARCH_HIT (no promotion).
    - Partial ISRCs (some tracks have no ISRC, ≥1 match, no mismatch) → ISRC_MATCH (promotion).
    - All inconclusive (no track has a source ISRC) → SEARCH_HIT (all-inconclusive rule).
    """

    def _patch_mb_for_run(self, mocker: MockerFixture, release: MBRelease) -> None:
        """Patch all MB API calls and internal helpers used by run().

        :param mocker: pytest-mock fixture.
        :param release: MBRelease model to return from fetch_release.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())
        mocker.patch("music_annotator._pipeline.fetch_recording_detail", return_value=MBRecording())
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

    def test_isrc_all_match_yields_full_verified(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """All source ISRCs match → CensusSignal.ISRC_MATCH → full-mb-verified, needs_spot_check=False.

        Single-medium release; source FLACs carry ISRC tags that appear in the corresponding
        recording's isrc_list; no embedded MBID, no TOC.  Asserts the signal is ISRC_MATCH and
        the provenance sidecar carries full-mb-verified + needs_spot_check=False.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/isrc-all-match")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))

        # Write two FLACs with ISRC tags matching the release's recording ISRC lists.
        for i, isrc in enumerate(["USRC12345678", "USRC87654321"], start=1):
            flac_path = src / f"0{i}.flac"
            fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
            audio = FLAC(str(flac_path))
            audio["isrc"] = [isrc]
            audio.save()

        release = _make_release_with_isrcs([["USRC12345678"], ["USRC87654321"]])
        self._patch_mb_for_run(mocker, release)

        log_events: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._pipeline.log.info",
            side_effect=lambda event, **kw: log_events.append({"event": event, **kw}),
        )

        music_annotator.run(
            release_id="rel-isrc",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )

        tier_events = [e for e in log_events if e["event"] == "annotation_tier_signal"]
        assert tier_events, "annotation_tier_signal must be logged"
        assert tier_events[0]["signal"] == CensusSignal.ISRC_MATCH

        flac_files = list(dest.rglob("*.flac"))
        assert flac_files
        work_top = _work_top_dir(Path(flac_files[0]), dest)
        prov_path = work_top / PROVENANCE_FILENAME
        result = _read_provenance_sidecar(prov_path)
        assert result.annotation_tier == AnnotationTier.FULL_MB_VERIFIED
        assert result.needs_spot_check is False

    def test_isrc_mismatch_stays_search_resolved(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """One source ISRC not in the candidate list → SEARCH_HIT (no promotion).

        The first track's ISRC matches; the second track's ISRC does not appear in the recording's
        isrc_list.  A single mismatch drops the whole dir to SEARCH_HIT.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/isrc-mismatch")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))

        # Track 1 matches; track 2 carries a wrong ISRC.
        for i, isrc in enumerate(["USRC12345678", "USRC00000000"], start=1):
            flac_path = src / f"0{i}.flac"
            fs.create_file(str(flac_path), contents=_MINIMAL_FLAC)
            audio = FLAC(str(flac_path))
            audio["isrc"] = [isrc]
            audio.save()

        # Track 2's recording has "USRC99999999", not "USRC00000000" → mismatch.
        release = _make_release_with_isrcs([["USRC12345678"], ["USRC99999999"]])
        self._patch_mb_for_run(mocker, release)

        log_events: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._pipeline.log.info",
            side_effect=lambda event, **kw: log_events.append({"event": event, **kw}),
        )

        music_annotator.run(
            release_id="rel-isrc",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )

        tier_events = [e for e in log_events if e["event"] == "annotation_tier_signal"]
        assert tier_events, "annotation_tier_signal must be logged"
        assert tier_events[0]["signal"] == CensusSignal.SEARCH_HIT

    def test_isrc_partial_no_mismatch_promotes(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Partial ISRCs (some tracks have no ISRC, ≥1 match, no mismatch) → ISRC_MATCH.

        Track 1 has a matching ISRC; track 2 has no ISRC tag (inconclusive, match=None).
        The partial-ISRC dir still promotes because no mismatch and ≥1 confirmed match.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/isrc-partial")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))

        # Track 1: ISRC tag present and matching.
        flac1 = src / "01.flac"
        fs.create_file(str(flac1), contents=_MINIMAL_FLAC)
        audio1 = FLAC(str(flac1))
        audio1["isrc"] = ["USRC12345678"]
        audio1.save()

        # Track 2: no ISRC tag → inconclusive (match=None).
        flac2 = src / "02.flac"
        fs.create_file(str(flac2), contents=_MINIMAL_FLAC)

        # Track 2's recording has an isrc_list, but the source has no ISRC → inconclusive.
        release = _make_release_with_isrcs([["USRC12345678"], ["USRC99999999"]])
        self._patch_mb_for_run(mocker, release)

        log_events: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._pipeline.log.info",
            side_effect=lambda event, **kw: log_events.append({"event": event, **kw}),
        )

        music_annotator.run(
            release_id="rel-isrc",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )

        tier_events = [e for e in log_events if e["event"] == "annotation_tier_signal"]
        assert tier_events, "annotation_tier_signal must be logged"
        assert tier_events[0]["signal"] == CensusSignal.ISRC_MATCH

    def test_isrc_all_inconclusive_stays_search_hit(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """All tracks inconclusive (no source ISRC) → SEARCH_HIT (all-inconclusive rule).

        No source file carries an ISRC tag; all _isrc_matches calls return match=None.
        The all-inconclusive dir does not promote — it stays at SEARCH_HIT.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src/isrc-all-inconclusive")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))

        # Two FLACs with no ISRC tags.
        for i in range(1, 3):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)

        # Recordings have ISRCs, but source files don't → all inconclusive.
        release = _make_release_with_isrcs([["USRC12345678"], ["USRC87654321"]])
        self._patch_mb_for_run(mocker, release)

        log_events: list[dict[str, object]] = []
        mocker.patch(
            "music_annotator._pipeline.log.info",
            side_effect=lambda event, **kw: log_events.append({"event": event, **kw}),
        )

        music_annotator.run(
            release_id="rel-isrc",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=False,
        )

        tier_events = [e for e in log_events if e["event"] == "annotation_tier_signal"]
        assert tier_events, "annotation_tier_signal must be logged"
        assert tier_events[0]["signal"] == CensusSignal.SEARCH_HIT


# ---------------------------------------------------------------------------
# AccurateRip tag round-trip KAT (C-AR)
# ---------------------------------------------------------------------------


class TestAccurateRipTagRoundtrip:
    """KAT: AccurateRip flat fields survive the mutagen write-and-read-back path.

    Pins C-AR: the 11 flat AccurateRip fields on TrackTags are written to FLAC (Vorbis Comment)
    and MP3 (TXXX) by apply_tags_flac / apply_tags_mp3, and read back identically by
    _read_tags_flac / _read_tags_mp3.  Uses the real mutagen path (no mocks).
    """

    def _ar_tags(self) -> TrackTags:
        """Build a TrackTags with populated v1 exact-match + v2 no-match AccurateRip fields.

        :returns: A :class:`~music_annotator.models.TrackTags` instance with AR fields set.
        """
        return TrackTags(
            title="Allegro",
            tracknumber="1",
            # v1: exact match, confidence 42, matching CRCs
            accuraterip_v1_result=AccurateRipResult.EXACT_MATCH,
            accuraterip_v1_confidence="42",
            accuraterip_v1_local_crc="AABB1122",
            accuraterip_v1_remote_crc="AABB1122",
            # v2: no exact match, confidence 5, differing CRCs
            accuraterip_v2_result=AccurateRipResult.NO_EXACT_MATCH,
            accuraterip_v2_confidence="5",
            accuraterip_v2_local_crc="CCDD3344",
            accuraterip_v2_remote_crc="EEFF5566",
            # rip CRCs and status
            accuraterip_test_crc="12345678",
            accuraterip_copy_crc="12345678",
            accuraterip_status="Copy OK",
        )

    def test_accuraterip_track_tag_roundtrip_flac(self, fs: FakeFilesystem) -> None:
        """AccurateRip flat fields survive apply_tags_flac → _read_tags_flac round-trip.

        Writes a TrackTags with populated v1 exact-match + v2 no-match AR fields to a FLAC file
        using the real mutagen path, reads back with _read_tags_flac, and asserts equality with
        to_file_dict() including all 11 AccurateRip keys.

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.flac")
        fs.create_dir("/out")
        fs.create_file(str(path), contents=_MINIMAL_FLAC)
        tags = self._ar_tags()
        apply_tags_flac(path, tags)
        read_back = _read_tags_flac(path)
        expected = tags.to_file_dict()
        # Assert all 11 AR keys are present and correct
        for key in (
            "ACCURATERIP_V1_RESULT",
            "ACCURATERIP_V1_CONFIDENCE",
            "ACCURATERIP_V1_LOCAL_CRC",
            "ACCURATERIP_V1_REMOTE_CRC",
            "ACCURATERIP_V2_RESULT",
            "ACCURATERIP_V2_CONFIDENCE",
            "ACCURATERIP_V2_LOCAL_CRC",
            "ACCURATERIP_V2_REMOTE_CRC",
            "ACCURATERIP_TEST_CRC",
            "ACCURATERIP_COPY_CRC",
            "ACCURATERIP_STATUS",
        ):
            assert read_back.get(key) == expected.get(key), f"FLAC round-trip mismatch for {key}"
        assert read_back == expected

    def test_accuraterip_track_tag_roundtrip_mp3(self, fs: FakeFilesystem) -> None:
        """AccurateRip flat fields survive apply_tags_mp3 → _read_tags_mp3 round-trip.

        Writes a TrackTags with populated v1 exact-match + v2 no-match AR fields to an MP3 file
        using the real mutagen path, reads back with _read_tags_mp3, and asserts equality with
        to_file_dict() filtered to the writable MP3 key set (including all 11 AccurateRip TXXX keys).

        :param fs: pyfakefs fixture.
        """
        path = Path("/out/track.mp3")
        fs.create_dir("/out")
        fs.create_file(str(path), contents=_MINIMAL_MP3)
        tags = self._ar_tags()
        apply_tags_mp3(path, tags)
        read_back = _read_tags_mp3(path)
        # pylint: disable-next=protected-access
        writable = music_annotator._tagger._MP3_STD_KEYS | frozenset(music_annotator._tagger._MP3_TXXX_MAP)
        expected = {k: v for k, v in tags.to_file_dict().items() if k in writable}
        # Assert all 11 AR keys are present and correct
        for key in (
            "ACCURATERIP_V1_RESULT",
            "ACCURATERIP_V1_CONFIDENCE",
            "ACCURATERIP_V1_LOCAL_CRC",
            "ACCURATERIP_V1_REMOTE_CRC",
            "ACCURATERIP_V2_RESULT",
            "ACCURATERIP_V2_CONFIDENCE",
            "ACCURATERIP_V2_LOCAL_CRC",
            "ACCURATERIP_V2_REMOTE_CRC",
            "ACCURATERIP_TEST_CRC",
            "ACCURATERIP_COPY_CRC",
            "ACCURATERIP_STATUS",
        ):
            assert read_back.get(key) == expected.get(key), f"MP3 round-trip mismatch for {key}"
        assert read_back == expected


# ---------------------------------------------------------------------------
# parse_whipper_log KATs (C-AR + C-WHIP)
# ---------------------------------------------------------------------------

# Minimal whipper-log YAML body (without the trailing SHA-256 line).
# The body is the content over which the self-attesting SHA-256 is computed.
# Format mirrors whipper's WhipperLogger schema 1:1.
_WHIPPER_LOG_BODY_FULL = """\
Log created by: whipper 0.10.0
Log creation date: 2024-01-15T10:30:00
CD metadata:
  MusicBrainz Disc ID: TestDiscID123
  CDDB Disc ID: 1234abcd
Tracks:
  1:
    AccurateRip v1:
      Result: exact-match
      Confidence: 42
      Local CRC: AABB1122
      Remote CRC: AABB1122
    AccurateRip v2:
      Result: no-exact-match
      Confidence: 5
      Local CRC: CCDD3344
      Remote CRC: EEFF5566
    Test CRC: 12345678
    Copy CRC: 12345678
    Status: Copy OK
  2:
    AccurateRip v1:
      Result: exact-match
      Confidence: 10
      Local CRC: DEADBEEF
      Remote CRC: DEADBEEF
    AccurateRip v2:
      Result: exact-match
      Confidence: 8
      Local CRC: CAFEBABE
      Remote CRC: CAFEBABE
    Test CRC: 87654321
    Copy CRC: 87654321
    Status: Copy OK
Conclusive status report:
  AccurateRip summary: All tracks accurately ripped
  Accurately ripped: 2
  Tracks in AR database: 2
"""

_WHIPPER_LOG_BODY_NO_AR = """\
Log created by: whipper 0.10.0
Log creation date: 2024-01-15T10:30:00
CD metadata:
  MusicBrainz Disc ID: NoARDiscID
  CDDB Disc ID: "00000000"
Tracks:
  1:
    Test CRC: AAAABBBB
    Copy CRC: AAAABBBB
    Status: Copy OK
  2:
    Test CRC: CCCCDDDD
    Copy CRC: CCCCDDDD
    Status: Copy OK
Conclusive status report:
  AccurateRip summary: No tracks found in AccurateRip database
  Accurately ripped: 0
  Tracks in AR database: 0
"""

_WHIPPER_LOG_BODY_PARTIAL = """\
Log created by: whipper 0.10.0
Log creation date: 2024-01-15T10:30:00
CD metadata:
  MusicBrainz Disc ID: PartialDiscID
  CDDB Disc ID: abcdef01
Tracks:
  1:
    AccurateRip v1:
      Result: exact-match
      Confidence: 15
      Local CRC: 11223344
      Remote CRC: 11223344
    AccurateRip v2:
      Result: exact-match
      Confidence: 12
      Local CRC: 55667788
      Remote CRC: 55667788
    Test CRC: AABBCCDD
    Copy CRC: AABBCCDD
    Status: Copy OK
  2:
    AccurateRip v1:
      Result: no-exact-match
      Confidence: 3
      Local CRC: DEADBEEF
      Remote CRC: CAFEBABE
    AccurateRip v2:
      Result: not-present
      Confidence: 0
      Local CRC: ''
      Remote CRC: ''
    Test CRC: 99887766
    Copy CRC: 99887766
    Status: Copy OK
Conclusive status report:
  AccurateRip summary: 1 of 2 tracks accurately ripped
  Accurately ripped: 1
  Tracks in AR database: 2
"""


def _make_whipper_log(body: str) -> str:
    """Append the self-attesting SHA-256 hash line to a whipper log body.

    Computes the SHA-256 of the body bytes (UTF-8) and appends the trailing
    ``SHA-256 hash: <UPPERHEX>`` line, matching whipper's ``WhipperLogger.logRip`` behaviour.

    :param body: The YAML body text (everything before the hash line).
    :returns: The complete whipper log text including the trailing SHA-256 line.
    """
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest().upper()
    return body + f"SHA-256 hash: {digest}\n"


class TestParseWhipperLogFull:
    """KAT: parse_whipper_log against a minimal all-accurate whipper log fixture.

    Covers: both tracks exact-match on v1 and v2; summary counts; MB/CDDB disc IDs;
    SHA-256 verification (matching hash); per-track CRCs and status.
    """

    def test_parse_whipper_log_full(self, fs: FakeFilesystem) -> None:
        """parse_whipper_log returns correct AccurateRipSummary and per-track dict for all-accurate case.

        Writes a minimal whipper log with two tracks (both exact-match on v1 and v2) to a fake
        filesystem, calls parse_whipper_log, and asserts the summary and per-track data.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/album")
        fs.create_dir(str(src))
        log_text = _make_whipper_log(_WHIPPER_LOG_BODY_FULL)
        fs.create_file(str(src / "album.log"), contents=log_text)

        summary, tracks = parse_whipper_log(src)

        # Summary fields
        assert summary.mb_disc_id == "TestDiscID123"
        assert summary.cddb_disc_id == "1234abcd"
        assert summary.accurately_ripped == 2
        assert summary.in_ar_database == 2
        assert summary.summary_text == "All tracks accurately ripped"
        assert len(summary.log_sha256) == 64
        assert summary.log_sha256 == summary.log_sha256.upper()
        assert summary.is_populated()

        # Track 1: v1 exact-match, v2 no-exact-match
        assert 1 in tracks
        t1 = tracks[1]
        assert t1.v1.version == "v1"
        assert t1.v1.result is AccurateRipResult.EXACT_MATCH
        assert t1.v1.confidence == 42
        assert t1.v1.local_crc == "AABB1122"
        assert t1.v1.remote_crc == "AABB1122"
        assert t1.v2.version == "v2"
        assert t1.v2.result is AccurateRipResult.NO_EXACT_MATCH
        assert t1.v2.confidence == 5
        assert t1.v2.local_crc == "CCDD3344"
        assert t1.v2.remote_crc == "EEFF5566"
        assert t1.test_crc == "12345678"
        assert t1.copy_crc == "12345678"
        assert t1.status == "Copy OK"

        # Track 2: v1 and v2 exact-match
        assert 2 in tracks
        t2 = tracks[2]
        assert t2.v1.result is AccurateRipResult.EXACT_MATCH
        assert t2.v1.confidence == 10
        assert t2.v2.result is AccurateRipResult.EXACT_MATCH
        assert t2.v2.confidence == 8
        assert t2.test_crc == "87654321"
        assert t2.copy_crc == "87654321"
        assert t2.status == "Copy OK"

    def test_parse_whipper_log_sha256_verified(self, fs: FakeFilesystem) -> None:
        """parse_whipper_log verifies the self-attesting SHA-256 without warning when it matches.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/album")
        fs.create_dir(str(src))
        log_text = _make_whipper_log(_WHIPPER_LOG_BODY_FULL)
        fs.create_file(str(src / "album.log"), contents=log_text)

        # Should not raise; SHA-256 matches
        summary, tracks = parse_whipper_log(src)
        assert summary.log_sha256 != ""
        assert len(tracks) == 2

    def test_parse_whipper_log_sha256_mismatch_warns(self, fs: FakeFilesystem, mocker: MockerFixture) -> None:
        """parse_whipper_log logs a WARNING when the SHA-256 does not match the body hash.

        A mismatch means the log was edited after whipper wrote it.  The dir is still recognised
        as whipper and the parse continues (not a hard failure per C-WHIP).

        :param fs: pyfakefs fixture.
        :param mocker: pytest-mock fixture.
        """
        src = Path("/src/album")
        fs.create_dir(str(src))
        # Corrupt the hash line so it doesn't match the body
        body = _WHIPPER_LOG_BODY_FULL
        bad_hash = "A" * 64
        log_text = body + f"SHA-256 hash: {bad_hash}\n"
        fs.create_file(str(src / "album.log"), contents=log_text)

        mock_log = mocker.patch("music_annotator._pipeline_io.log")
        summary, tracks = parse_whipper_log(src)

        mock_log.warning.assert_called_once()
        call_kwargs = mock_log.warning.call_args
        assert call_kwargs[0][0] == "whipper_log_sha256_mismatch"
        # Parse still succeeds despite mismatch
        assert summary.mb_disc_id == "TestDiscID123"
        assert len(tracks) == 2

    def test_parse_whipper_log_no_log_raises(self, fs: FakeFilesystem) -> None:
        """parse_whipper_log raises FileNotFoundError when no whipper log is present.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/album")
        fs.create_dir(str(src))
        # No .log file at all
        with pytest.raises(FileNotFoundError):
            parse_whipper_log(src)

    def test_parse_whipper_log_plain_log_no_sha256_raises(self, fs: FakeFilesystem) -> None:
        """parse_whipper_log raises FileNotFoundError for a .log without the trailing SHA-256 line.

        A plain .log without the C-WHIP strong signature (1) is not a whipper native log.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "rip.log"), contents="some other ripper log\nno sha256 here\n")
        with pytest.raises(FileNotFoundError):
            parse_whipper_log(src)


class TestParseWhipperLogNoARDatabase:
    """KAT: parse_whipper_log when no tracks are in the AccurateRip database.

    Covers: tracks with no AccurateRip blocks; summary counts are zero; summary_text set.
    """

    def test_parse_whipper_log_no_ar_database(self, fs: FakeFilesystem) -> None:
        """parse_whipper_log returns zero AR counts and not-present results when no tracks are in the DB.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/album")
        fs.create_dir(str(src))
        log_text = _make_whipper_log(_WHIPPER_LOG_BODY_NO_AR)
        fs.create_file(str(src / "album.log"), contents=log_text)

        summary, tracks = parse_whipper_log(src)

        assert summary.mb_disc_id == "NoARDiscID"
        assert summary.cddb_disc_id == "00000000"
        assert summary.accurately_ripped == 0
        assert summary.in_ar_database == 0
        assert "No tracks found" in summary.summary_text

        # Both tracks present but with default NOT_PRESENT AR results
        assert 1 in tracks
        assert 2 in tracks
        assert tracks[1].v1.result is AccurateRipResult.NOT_PRESENT
        assert tracks[1].v2.result is AccurateRipResult.NOT_PRESENT
        assert tracks[1].v1.confidence == 0
        assert tracks[1].v1.local_crc == ""
        assert tracks[1].v1.remote_crc == ""
        assert tracks[1].test_crc == "AAAABBBB"
        assert tracks[1].copy_crc == "AAAABBBB"
        assert tracks[1].status == "Copy OK"


class TestParseWhipperLogPartialMatch:
    """KAT: parse_whipper_log with mixed AR results (some exact-match, some no-match, some not-present).

    Covers: track 1 exact-match on both versions; track 2 no-exact-match on v1, not-present on v2.
    """

    def test_parse_whipper_log_partial_match(self, fs: FakeFilesystem) -> None:
        """parse_whipper_log correctly parses mixed AR results across tracks.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/album")
        fs.create_dir(str(src))
        log_text = _make_whipper_log(_WHIPPER_LOG_BODY_PARTIAL)
        fs.create_file(str(src / "album.log"), contents=log_text)

        summary, tracks = parse_whipper_log(src)

        assert summary.accurately_ripped == 1
        assert summary.in_ar_database == 2
        assert "1 of 2" in summary.summary_text

        # Track 1: exact-match on both
        assert tracks[1].v1.result is AccurateRipResult.EXACT_MATCH
        assert tracks[1].v1.confidence == 15
        assert tracks[1].v2.result is AccurateRipResult.EXACT_MATCH
        assert tracks[1].v2.confidence == 12

        # Track 2: no-exact-match on v1, not-present on v2
        assert tracks[2].v1.result is AccurateRipResult.NO_EXACT_MATCH
        assert tracks[2].v1.confidence == 3
        assert tracks[2].v1.local_crc == "DEADBEEF"
        assert tracks[2].v1.remote_crc == "CAFEBABE"
        assert tracks[2].v2.result is AccurateRipResult.NOT_PRESENT
        assert tracks[2].v2.confidence == 0


class TestParseWhipperLogHelpers:
    """Unit tests for internal parse helpers: _find_whipper_log, _parse_ar_track_result, _parse_ar_track."""

    def test_find_whipper_log_returns_none_when_absent(self, fs: FakeFilesystem) -> None:
        """_find_whipper_log returns None when no .log file is present.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/album")
        fs.create_dir(str(src))
        assert _find_whipper_log(src) is None

    def test_find_whipper_log_returns_none_for_non_whipper_log(self, fs: FakeFilesystem) -> None:
        """_find_whipper_log returns None for a .log without the trailing SHA-256 line.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/album")
        fs.create_dir(str(src))
        fs.create_file(str(src / "eac.log"), contents="EAC log content\nno sha256\n")
        assert _find_whipper_log(src) is None

    def test_find_whipper_log_returns_path_for_whipper_log(self, fs: FakeFilesystem) -> None:
        """_find_whipper_log returns the path of a valid whipper log.

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/album")
        fs.create_dir(str(src))
        log_text = _make_whipper_log(_WHIPPER_LOG_BODY_FULL)
        fs.create_file(str(src / "album.log"), contents=log_text)
        result = _find_whipper_log(src)
        assert result is not None
        assert result.name == "album.log"

    def test_parse_ar_track_result_missing_block(self) -> None:
        """_parse_ar_track_result returns NOT_PRESENT defaults when block is None.

        :returns: None.
        """
        result = _parse_ar_track_result("v1", None)
        assert result.version == "v1"
        assert result.result is AccurateRipResult.NOT_PRESENT
        assert result.confidence == 0
        assert result.local_crc == ""
        assert result.remote_crc == ""

    def test_parse_ar_track_result_invalid_result_string(self) -> None:
        """_parse_ar_track_result falls back to NOT_PRESENT for an unrecognised Result string.

        :returns: None.
        """
        block = {"Result": "unknown-value", "Confidence": 0, "Local CRC": "", "Remote CRC": ""}
        result = _parse_ar_track_result("v2", block)
        assert result.result is AccurateRipResult.NOT_PRESENT

    def test_parse_ar_track_non_dict_yields_defaults(self) -> None:
        """_parse_ar_track returns default AccurateRipTrack when track_dict is not a dict.

        :returns: None.
        """
        track = _parse_ar_track("not a dict")
        assert track.v1.result is AccurateRipResult.NOT_PRESENT
        assert track.v2.result is AccurateRipResult.NOT_PRESENT
        assert track.test_crc == ""
        assert track.copy_crc == ""
        assert track.status == ""

    def test_parse_whipper_log_htoa_track_zero(self, fs: FakeFilesystem) -> None:
        """parse_whipper_log maps HTOA track 0 to key 0 in the returned dict.

        Whipper may emit a track keyed 0 (hidden track one audio).  It is mapped to key 0
        in the returned dict, not skipped.

        :param fs: pyfakefs fixture.
        """
        body = """\
Log created by: whipper 0.10.0
CD metadata:
  MusicBrainz Disc ID: HTOADisc
  CDDB Disc ID: "00000001"
Tracks:
  0:
    Test CRC: "00000000"
    Copy CRC: "00000000"
    Status: Copy OK
  1:
    AccurateRip v1:
      Result: exact-match
      Confidence: 5
      Local CRC: AABBCCDD
      Remote CRC: AABBCCDD
    AccurateRip v2:
      Result: exact-match
      Confidence: 3
      Local CRC: EEFF0011
      Remote CRC: EEFF0011
    Test CRC: AABBCCDD
    Copy CRC: AABBCCDD
    Status: Copy OK
Conclusive status report:
  AccurateRip summary: All tracks accurately ripped
  Accurately ripped: 1
  Tracks in AR database: 1
"""
        src = Path("/src/album")
        fs.create_dir(str(src))
        log_text = _make_whipper_log(body)
        fs.create_file(str(src / "album.log"), contents=log_text)

        _summary, tracks = parse_whipper_log(src)

        # HTOA track 0 is present in the dict
        assert 0 in tracks
        assert tracks[0].test_crc == "00000000"
        assert tracks[0].v1.result is AccurateRipResult.NOT_PRESENT
        # Regular track 1 is also present
        assert 1 in tracks
        assert tracks[1].v1.result is AccurateRipResult.EXACT_MATCH

    def test_find_whipper_log_oserror_skips_file(self, fs: FakeFilesystem) -> None:
        """_find_whipper_log skips a .log file that raises OSError on read and returns None.

        Exercises the OSError branch in _find_whipper_log (lines 996-997).

        :param fs: pyfakefs fixture.
        """
        src = Path("/src/album")
        fs.create_dir(str(src))
        # Create a .log file but make it unreadable by removing read permission
        log_path = src / "unreadable.log"
        fs.create_file(str(log_path), contents="some content")
        os.chmod(str(log_path), 0o000)
        result = _find_whipper_log(src)
        assert result is None

    def test_parse_whipper_log_minimal_yaml_body(self, fs: FakeFilesystem) -> None:
        """parse_whipper_log handles a log with minimal YAML body (no CD metadata, no tracks, no status).

        Exercises the branches where cd_meta, status_report, and tracks_raw are not dicts.

        :param fs: pyfakefs fixture.
        """
        # Minimal body with only the required SHA-256 structure but no meaningful YAML content
        body = "Log created by: whipper 0.10.0\n"
        src = Path("/src/album")
        fs.create_dir(str(src))
        log_text = _make_whipper_log(body)
        fs.create_file(str(src / "album.log"), contents=log_text)

        summary, tracks = parse_whipper_log(src)

        # All fields default to empty/zero when blocks are absent
        assert summary.mb_disc_id == ""
        assert summary.cddb_disc_id == ""
        assert summary.accurately_ripped == 0
        assert summary.in_ar_database == 0
        assert summary.summary_text == ""
        assert tracks == {}

    def test_parse_whipper_log_non_dict_yaml_body(self, fs: FakeFilesystem) -> None:
        """parse_whipper_log handles a log whose YAML body parses to a non-dict (e.g. a list).

        Exercises the doc = {} fallback branch when yaml.safe_load returns a non-dict.

        :param fs: pyfakefs fixture.
        """
        # YAML body that parses to a list, not a dict
        body = "- item1\n- item2\n"
        src = Path("/src/album")
        fs.create_dir(str(src))
        log_text = _make_whipper_log(body)
        fs.create_file(str(src / "album.log"), contents=log_text)

        summary, tracks = parse_whipper_log(src)

        assert summary.mb_disc_id == ""
        assert tracks == {}

    def test_parse_whipper_log_non_integer_track_key(self, fs: FakeFilesystem) -> None:
        """parse_whipper_log skips track entries with non-integer keys.

        Exercises the ValueError/TypeError branch in the track-key conversion loop.

        :param fs: pyfakefs fixture.
        """
        body = """\
Log created by: whipper 0.10.0
CD metadata:
  MusicBrainz Disc ID: TestDisc
  CDDB Disc ID: 12345678
Tracks:
  not_a_number:
    Test CRC: AABBCCDD
    Copy CRC: AABBCCDD
    Status: Copy OK
  1:
    AccurateRip v1:
      Result: exact-match
      Confidence: 5
      Local CRC: AABBCCDD
      Remote CRC: AABBCCDD
    AccurateRip v2:
      Result: exact-match
      Confidence: 3
      Local CRC: EEFF0011
      Remote CRC: EEFF0011
    Test CRC: AABBCCDD
    Copy CRC: AABBCCDD
    Status: Copy OK
Conclusive status report:
  AccurateRip summary: All tracks accurately ripped
  Accurately ripped: 1
  Tracks in AR database: 1
"""
        src = Path("/src/album")
        fs.create_dir(str(src))
        log_text = _make_whipper_log(body)
        fs.create_file(str(src / "album.log"), contents=log_text)

        _summary, tracks = parse_whipper_log(src)

        # The non-integer key is skipped; only track 1 is present
        assert "not_a_number" not in str(tracks)
        assert 1 in tracks
        assert tracks[1].v1.result is AccurateRipResult.EXACT_MATCH


# ---------------------------------------------------------------------------
# collect_applied_case_ids — unit tests
# ---------------------------------------------------------------------------


class TestCollectAppliedCaseIds:
    """Unit tests for :func:`music_annotator._tags.collect_applied_case_ids`.

    Verifies that each contested-default case-ID is emitted exactly when its decision site fires
    and not emitted when it does not.
    """

    def test_sel11_emitted_when_soloists_present(self) -> None:
        """SEL-11 is emitted when cea_soloist_names is non-empty (soloists identified but not in path).

        :returns: None.
        """
        tags = TrackTags(cea_soloist_names="Hilary Hahn")
        result = collect_applied_case_ids(tags)
        assert "SEL-11" in result

    def test_sel11_absent_when_no_soloists(self) -> None:
        """SEL-11 is absent when cea_soloist_names is empty (no soloists identified).

        :returns: None.
        """
        tags = TrackTags(cea_soloist_names="")
        result = collect_applied_case_ids(tags)
        assert "SEL-11" not in result

    def test_rend1_rend2_emitted_when_composer_and_classical(self) -> None:
        """REND-1 and REND-2 are emitted when composer is non-empty and is_classical is '1'.

        :returns: None.
        """
        tags = TrackTags(composer="Beethoven", is_classical="1")
        result = collect_applied_case_ids(tags)
        assert "REND-1" in result
        assert "REND-2" in result

    def test_rend1_rend2_absent_when_no_composer(self) -> None:
        """REND-1 and REND-2 are absent when composer is empty.

        :returns: None.
        """
        tags = TrackTags(composer="", is_classical="1")
        result = collect_applied_case_ids(tags)
        assert "REND-1" not in result
        assert "REND-2" not in result

    def test_rend1_rend2_absent_when_not_classical(self) -> None:
        """REND-1 and REND-2 are absent when is_classical is '0' (non-classical release).

        :returns: None.
        """
        tags = TrackTags(composer="Beethoven", is_classical="0")
        result = collect_applied_case_ids(tags)
        assert "REND-1" not in result
        assert "REND-2" not in result

    def test_rend14_emitted_when_conductor_present(self) -> None:
        """REND-14 is emitted when conductor is non-empty (billing-order composite assembled).

        :returns: None.
        """
        tags = TrackTags(conductor="Karajan")
        result = collect_applied_case_ids(tags)
        assert "REND-14" in result

    def test_rend14_emitted_when_ensemble_present(self) -> None:
        """REND-14 is emitted when cea_ensembles is non-empty (billing-order composite assembled).

        :returns: None.
        """
        tags = TrackTags(cea_ensembles="Berliner Philharmoniker")
        result = collect_applied_case_ids(tags)
        assert "REND-14" in result

    def test_rend14_emitted_when_soloist_present(self) -> None:
        """REND-14 is emitted when cea_soloist_names is non-empty (billing-order composite assembled).

        :returns: None.
        """
        tags = TrackTags(cea_soloist_names="Hilary Hahn")
        result = collect_applied_case_ids(tags)
        assert "REND-14" in result

    def test_rend14_absent_when_no_performers(self) -> None:
        """REND-14 is absent when no performers are classified (no soloists, conductor, or ensemble).

        :returns: None.
        """
        tags = TrackTags(cea_soloist_names="", conductor="", cea_ensembles="")
        result = collect_applied_case_ids(tags)
        assert "REND-14" not in result

    def test_empty_tags_returns_empty_list(self) -> None:
        """An empty TrackTags (no performers, no composer) returns an empty case-ID list.

        :returns: None.
        """
        tags = TrackTags()
        result = collect_applied_case_ids(tags)
        assert result == []

    def test_all_structural_cases_for_classical_with_conductor(self) -> None:
        """A classical release with composer and conductor emits REND-1, REND-2, and REND-14.

        :returns: None.
        """
        tags = TrackTags(composer="Beethoven", is_classical="1", conductor="Karajan")
        result = collect_applied_case_ids(tags)
        assert "REND-1" in result
        assert "REND-2" in result
        assert "REND-14" in result
        assert "SEL-11" not in result

    def test_concerto_case_emits_sel11_and_structural(self) -> None:
        """A concerto release with soloist, composer, and conductor emits SEL-11 plus structural cases.

        :returns: None.
        """
        tags = TrackTags(
            composer="Beethoven",
            is_classical="1",
            conductor="Karajan",
            cea_soloist_names="Hilary Hahn",
        )
        result = collect_applied_case_ids(tags)
        assert "SEL-11" in result
        assert "REND-1" in result
        assert "REND-2" in result
        assert "REND-14" in result


# ---------------------------------------------------------------------------
# KAT: applied_case_ids threaded to provenance sidecar via run()
# ---------------------------------------------------------------------------


class TestAppliedCaseIdsInSidecar:
    """KAT: applied contested-default case-IDs are written to the provenance sidecar by run().

    Verifies the end-to-end threading from decision sites in build_track_tags / collect_applied_case_ids
    through the pipeline accumulator to ProvenanceSidecar.applied_case_ids in the on-disk sidecar.

    Three shapes are tested:
    - Concerto release with a named soloist: SEL-11 present.
    - Plain single-composer classical release (no soloists): SEL-11 absent; structural cases present.
    - Multi-track work dir: case-IDs from all tracks are unioned into the sidecar.
    """

    def _patch_mb_base(self, mocker: MockerFixture, release: MBRelease) -> None:
        """Patch MB API infrastructure (useragent, release, cover art, acoustid, fpcalc, verify).

        :param mocker: pytest-mock fixture.
        :param release: MBRelease model to return from fetch_release.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

    def _make_classical_work(self, work_id: str, title: str, composer_id: str, composer_name: str) -> MBWork:
        """Build a minimal classical work with a composer relation.

        :param work_id: MBID for the work.
        :param title: Work title.
        :param composer_id: MBID for the composer artist.
        :param composer_name: Display name for the composer artist.
        :returns: An :class:`~music_annotator.models.MBWork` instance.
        """
        return _w(
            {
                "id": work_id,
                "title": title,
                "type": "Classical",
                "artist-relation-list": [
                    {
                        "type": "composer",
                        "artist": {"id": composer_id, "name": composer_name, "sort-name": f"{composer_name}, Ludwig"},
                        "attribute-list": [],
                    }
                ],
                "work-relation-list": [],
                "attribute-list": [],
                "tag-list": [],
            }
        )

    def test_concerto_release_sidecar_contains_sel11(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """A concerto release with a named soloist writes SEL-11 to the provenance sidecar.

        The soloist is identified in build_cea_performers (goes to instrumentalists) and
        collect_applied_case_ids emits SEL-11 because cea_soloist_names is non-empty.
        The pipeline accumulator threads this to ProvenanceSidecar.applied_case_ids.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=1)
        self._patch_mb_base(mocker, release)

        work = self._make_classical_work("w-concerto", "Violin Concerto", "a-beethoven", "Beethoven")

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            return _rec(
                {
                    "id": rec_id,
                    "title": "Allegro",
                    "artist-credit": [],
                    "artist-relation-list": [
                        {
                            "type": "performer",
                            "direction": "backward",
                            "artist": {"id": "a-hahn", "name": "Hilary Hahn", "sort-name": "Hahn, Hilary"},
                            "attribute-list": [{"type": "instrument", "value": "violin"}],
                        },
                        {
                            "type": "conductor",
                            "direction": "backward",
                            "artist": {"id": "a-karajan", "name": "Karajan", "sort-name": "Karajan, Herbert von"},
                            "attribute-list": [],
                        },
                    ],
                    "work-relation-list": [{"type": "performance", "work": {"id": "w-concerto", "title": "Violin Concerto"}}],
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=work)
        mocker.patch("music_annotator._works.fetch_work_detail", return_value=work)

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        flac_files = list(dest.rglob("*.flac"))
        assert flac_files, "Expected at least one FLAC in dest"
        work_top = _work_top_dir(Path(flac_files[0]), dest)
        prov_path = work_top / PROVENANCE_FILENAME
        result = _read_provenance_sidecar(prov_path)
        assert "SEL-11" in result.applied_case_ids, (
            f"Expected SEL-11 in applied_case_ids for concerto release; got {result.applied_case_ids}"
        )

    def test_plain_classical_release_no_sel11(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """A plain single-composer classical release (no soloists) has SEL-11 absent.

        The structural cases REND-1 and REND-2 are present (composer identified, classical release).
        REND-14 is present (conductor classified).  SEL-11 is absent (no soloists).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        fs.create_file(str(src / "01.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=1)
        self._patch_mb_base(mocker, release)

        work = self._make_classical_work("w-symphony", "Symphony No. 5", "a-beethoven", "Beethoven")

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            return _rec(
                {
                    "id": rec_id,
                    "title": "Allegro con brio",
                    "artist-credit": [],
                    "artist-relation-list": [
                        {
                            "type": "conductor",
                            "direction": "backward",
                            "artist": {"id": "a-karajan", "name": "Karajan", "sort-name": "Karajan, Herbert von"},
                            "attribute-list": [],
                        },
                    ],
                    "work-relation-list": [{"type": "performance", "work": {"id": "w-symphony", "title": "Symphony No. 5"}}],
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=work)
        mocker.patch("music_annotator._works.fetch_work_detail", return_value=work)

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        flac_files = list(dest.rglob("*.flac"))
        assert flac_files, "Expected at least one FLAC in dest"
        work_top = _work_top_dir(Path(flac_files[0]), dest)
        prov_path = work_top / PROVENANCE_FILENAME
        result = _read_provenance_sidecar(prov_path)
        assert "SEL-11" not in result.applied_case_ids, (
            f"SEL-11 must be absent for a plain classical release without soloists; got {result.applied_case_ids}"
        )
        assert "REND-1" in result.applied_case_ids, (
            f"Expected REND-1 in applied_case_ids for classical release with composer; got {result.applied_case_ids}"
        )
        assert "REND-2" in result.applied_case_ids, (
            f"Expected REND-2 in applied_case_ids for classical release with composer; got {result.applied_case_ids}"
        )
        assert "REND-14" in result.applied_case_ids, (
            f"Expected REND-14 in applied_case_ids for classical release with conductor; got {result.applied_case_ids}"
        )

    def test_multi_track_work_dir_unions_case_ids_across_tracks(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """Case-IDs from all tracks in a work dir are unioned into the sidecar.

        Two tracks share the same work dir.  The first track has a soloist (SEL-11); the second
        does not.  The sidecar must carry SEL-11 (from the first track) after both are processed,
        demonstrating that the set-union merge captures contributions from all tracks.

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
        self._patch_mb_base(mocker, release)

        work = self._make_classical_work("w-concerto", "Violin Concerto", "a-beethoven", "Beethoven")

        call_count = [0]

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            call_count[0] += 1
            # Track 1: has a soloist (SEL-11 fires); track 2: conductor only (SEL-11 does not fire).
            conductor_rel: JSON = {
                "type": "conductor",
                "direction": "backward",
                "artist": {"id": "a-karajan", "name": "Karajan", "sort-name": "Karajan, Herbert von"},
                "attribute-list": [],
            }
            soloist_rel: JSON = {
                "type": "performer",
                "direction": "backward",
                "artist": {"id": "a-hahn", "name": "Hilary Hahn", "sort-name": "Hahn, Hilary"},
                "attribute-list": [{"type": "instrument", "value": "violin"}],
            }
            artist_rels: list[JSON] = [conductor_rel] if call_count[0] != 1 else [conductor_rel, soloist_rel]
            return _rec(
                {
                    "id": rec_id,
                    "title": "Movement",
                    "artist-credit": [],
                    "artist-relation-list": artist_rels,
                    "work-relation-list": [{"type": "performance", "work": {"id": "w-concerto", "title": "Violin Concerto"}}],
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=work)
        mocker.patch("music_annotator._works.fetch_work_detail", return_value=work)

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        flac_files = list(dest.rglob("*.flac"))
        assert flac_files, "Expected at least one FLAC in dest"
        work_top = _work_top_dir(Path(flac_files[0]), dest)
        prov_path = work_top / PROVENANCE_FILENAME
        result = _read_provenance_sidecar(prov_path)
        # SEL-11 was applied on track 1; the set-union merge must carry it to the sidecar.
        assert "SEL-11" in result.applied_case_ids, (
            f"Expected SEL-11 in applied_case_ids after multi-track union; got {result.applied_case_ids}"
        )


# ---------------------------------------------------------------------------
# run() — work-group modal depth threading (uniform-ceiling/ragged-floor rule)
# ---------------------------------------------------------------------------


class TestRunWorkGroupModalDepth:
    """Tests for the work-group modal depth threading in run().

    Verifies that run() computes the work-group modal depth once per group and passes it to
    build_dest_path, so over-resolved branches clamp to the group ceiling (Shape C/D) while
    uniform-depth groups are unchanged (no-regression).  A parity assertion guards that run()
    and repath() compute byte-identical paths for the same group.
    """

    @staticmethod
    def _make_classical_tags(
        cwp_part_levels: str,
        cwp_movt_num: str,
        title: str,
        *,
        extra_parts: dict[str, str] | None = None,
    ) -> TrackTags:
        """Build a minimal classical TrackTags with the given hierarchy depth.

        Sets the fields required for build_dest_path to produce a Classical path:
        cwp_work_top, cwp_worktype_genres_top, cwp_composer_lastnames, recording_date,
        cwp_workid_top, and cwp_part_levels.  Dynamic per-level extras (cwp_part_N,
        cwp_ordering_key_N) are passed via ``extra_parts``.

        :param cwp_part_levels: String value for CWP_PART_LEVELS (e.g. ``"2"`` or ``"3"``).
        :param cwp_movt_num: String value for CWP_MOVT_NUM (leaf movement number).
        :param title: Track title.
        :param extra_parts: Optional dict of lowercase model_extra keys to set (e.g.
            ``{"cwp_part_1": "Act I", "cwp_ordering_key_1": "1"}``).
        :returns: A :class:`~music_annotator.models.TrackTags` instance.
        """
        tags = TrackTags(
            cwp_work_top="Water Music",
            cwp_worktype_genres_top="Classical",
            cwp_composer_lastnames="Handel",
            recording_date="1970",
            cwp_workid_top="w-water-music",
            cwp_part_levels=cwp_part_levels,
            cwp_movt_num=cwp_movt_num,
            movementtotal="3",
            title=title,
            artist="Karajan",
        )
        if extra_parts and tags.model_extra is not None:
            tags.model_extra.update(extra_parts)
        return tags

    @staticmethod
    def _patch_run_base(mocker: MockerFixture, release: MBRelease, tags_by_pos: dict[int, TrackTags]) -> None:
        """Patch all MB API calls for a run() test with pre-built tags.

        Mocks fetch_release, fetch_cover_art, fetch_recording_detail, and build_track_tags so
        that run() uses the caller-supplied tags without making real MB API calls.  Also patches
        apply_tags_flac, _verify_copy, and _run_fpcalc to no-ops.

        :param mocker: pytest-mock fixture.
        :param release: MBRelease to return from fetch_release.
        :param tags_by_pos: Mapping from 1-based track position to pre-built TrackTags.
        """
        mocker.patch("music_annotator._mb_api.mb.set_useragent")
        mocker.patch("music_annotator._pipeline.fetch_release", return_value=release)
        mocker.patch("music_annotator._pipeline.fetch_cover_art", return_value=CoverArt())

        call_count = [0]

        def _fetch_rec(rec_id: str, no_cache: bool = False) -> MBRecording:  # pylint: disable=unused-argument
            call_count[0] += 1
            return _rec(
                {
                    "id": rec_id,
                    "title": f"Track {call_count[0]}",
                    "artist-credit": [],
                    "artist-relation-list": [],
                    "work-relation-list": [],
                }
            )

        mocker.patch("music_annotator._pipeline.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator._mb_api.fetch_work_detail", return_value=MBWork())

        # Inject pre-built tags: build_track_tags is called once per track in all_media_pairs
        # order (global index 0, 1, 2, …).  We supply tags keyed by 1-based position.
        pos_iter = [0]

        def _build_tags(*args: object, **kwargs: object) -> TrackTags:  # pylint: disable=unused-argument
            pos_iter[0] += 1
            return tags_by_pos.get(pos_iter[0], TrackTags())

        mocker.patch("music_annotator._pipeline.build_track_tags", side_effect=_build_tags)
        mocker.patch("music_annotator._pipeline.apply_tags_flac")
        mocker.patch("music_annotator._pipeline._verify_copy")  # pylint: disable=protected-access
        mocker.patch("music_annotator._pipeline._run_fpcalc", return_value="")

    def test_run_clamps_over_resolved_track_to_modal_depth(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """run() clamps a Shape-C/D over-resolved track to the work-group modal depth.

        A 3-track group where 2 tracks have CWP_PART_LEVELS=2 and 1 track has CWP_PART_LEVELS=3
        (Shape C: one over-resolved movement).  The modal depth is 2.  The PL=3 track must render
        at depth 2 (one intermediate directory), not depth 3 (two intermediate directories).

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 4):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=3)

        # Tracks 1 and 2: PL=2 (one intermediate directory: Act I)
        tags1 = self._make_classical_tags(
            "2",
            "1",
            "Allegro",
            extra_parts={"cwp_part_1": "Act I", "cwp_ordering_key_1": "1"},
        )
        tags2 = self._make_classical_tags(
            "2",
            "2",
            "Andante",
            extra_parts={"cwp_part_1": "Act I", "cwp_ordering_key_1": "1"},
        )
        # Track 3: PL=3 (two intermediate directories: Act I / Scene 1) — over-resolved
        tags3 = self._make_classical_tags(
            "3",
            "3",
            "Presto",
            extra_parts={
                "cwp_part_1": "Scene 1",
                "cwp_ordering_key_1": "1",
                "cwp_part_2": "Act I",
                "cwp_ordering_key_2": "1",
            },
        )

        self._patch_run_base(mocker, release, {1: tags1, 2: tags2, 3: tags3})

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        dest_files = sorted(dest.rglob("*.flac"))
        assert len(dest_files) == 3, f"Expected 3 FLAC files, got {len(dest_files)}"

        # All three tracks must land at depth 2 (one intermediate directory below work_dir).
        # Relative path structure: top_dir/work_dir/intermediate/leaf.flac (no class prefix — C-UNIVERSAL)
        # That is 4 parts (including the filename).  Depth 3 would be 5 parts.
        for f in dest_files:
            rel_parts = f.relative_to(dest).parts
            assert len(rel_parts) == 4, (  # noqa: PLR2004
                f"Expected 4 path parts (top/work/act/leaf) for {f.relative_to(dest)}, got {len(rel_parts)}: {rel_parts}"
            )

    def test_run_uniform_depth_group_unchanged(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """run() leaves a uniform-depth group unchanged (no-regression for the common case).

        A 2-track group where both tracks have CWP_PART_LEVELS=2.  The modal depth is 2.
        The clamp is a no-op (min(2, 2) = 2) and the paths are identical to pre-threading.

        :param mocker: pytest-mock fixture.
        :param fs: pyfakefs fixture.
        """
        src = Path("/src")
        dest = Path("/dest")
        fs.create_dir(str(src))
        fs.create_dir(str(dest))
        for i in range(1, 3):
            fs.create_file(str(src / f"0{i}.flac"), contents=_MINIMAL_FLAC)

        release = _make_release(n_tracks=2)

        tags1 = self._make_classical_tags(
            "2",
            "1",
            "Allegro",
            extra_parts={"cwp_part_1": "Act I", "cwp_ordering_key_1": "1"},
        )
        tags2 = self._make_classical_tags(
            "2",
            "2",
            "Andante",
            extra_parts={"cwp_part_1": "Act I", "cwp_ordering_key_1": "1"},
        )

        self._patch_run_base(mocker, release, {1: tags1, 2: tags2})

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )

        dest_files = sorted(dest.rglob("*.flac"))
        assert len(dest_files) == 2, f"Expected 2 FLAC files, got {len(dest_files)}"

        # Both tracks must land at depth 2 (one intermediate directory below work_dir).
        # Relative path structure: top_dir/work_dir/intermediate/leaf.flac = 4 parts (no class prefix — C-UNIVERSAL).
        for f in dest_files:
            rel_parts = f.relative_to(dest).parts
            assert len(rel_parts) == 4, (  # noqa: PLR2004
                f"Expected 4 path parts (top/work/act/leaf) for {f.relative_to(dest)}, got {len(rel_parts)}: {rel_parts}"
            )

    def test_run_repath_parity_same_group_same_path(self, fs: FakeFilesystem) -> None:
        """run() and repath() compute byte-identical paths for the same work-group.

        Guards ingest/maintenance parity: a group with mixed CWP_PART_LEVELS (modal=2, one PL=3
        track) must produce the same destination path whether computed by run() (from MB tags) or
        by repath() (from embedded tags).  The parity is verified by calling build_dest_path
        directly with the same tags and the modal depth that both callers compute.

        :param fs: pyfakefs fixture.
        """
        dest = Path("/dest")
        fs.create_dir(str(dest))

        # Build the same tags that run() would produce for a mixed-depth group.
        tags_pl2 = self._make_classical_tags(
            "2",
            "1",
            "Allegro",
            extra_parts={"cwp_part_1": "Act I", "cwp_ordering_key_1": "1"},
        )
        tags_pl3 = self._make_classical_tags(
            "3",
            "2",
            "Presto",
            extra_parts={
                "cwp_part_1": "Scene 1",
                "cwp_ordering_key_1": "1",
                "cwp_part_2": "Act I",
                "cwp_ordering_key_2": "1",
            },
        )

        # Compute the modal depth the same way run() and repath() both do:
        # group by cwp_workid_top, then call work_group_modal_depth.
        part_levels_list = [int(tags_pl2.cwp_part_levels or "0"), int(tags_pl3.cwp_part_levels or "0")]
        modal = work_group_modal_depth(part_levels_list)
        assert modal == 2, f"Expected modal depth 2, got {modal}"  # noqa: PLR2004

        # run()-side path: build_dest_path with group_modal_depth=modal
        run_path_pl2 = build_dest_path(dest, MBRelease(), MBTrack(), tags_pl2, group_modal_depth=modal)
        run_path_pl3 = build_dest_path(dest, MBRelease(), MBTrack(), tags_pl3, group_modal_depth=modal)

        # repath()-side path: same call — repath uses the same grouping and modal depth.
        repath_path_pl2 = build_dest_path(dest, MBRelease(), MBTrack(), tags_pl2, group_modal_depth=modal)
        repath_path_pl3 = build_dest_path(dest, MBRelease(), MBTrack(), tags_pl3, group_modal_depth=modal)

        # Parity: run and repath must produce byte-identical paths.
        assert run_path_pl2 == repath_path_pl2, f"Parity failure for PL=2 track: run={run_path_pl2} repath={repath_path_pl2}"
        assert run_path_pl3 == repath_path_pl3, f"Parity failure for PL=3 track: run={run_path_pl3} repath={repath_path_pl3}"

        # Clamp assertion: the PL=3 track must render at the same depth as the PL=2 track.
        # Both should have the same number of path components below dest.
        assert len(run_path_pl2.relative_to(dest).parts) == len(run_path_pl3.relative_to(dest).parts), (
            f"Depth mismatch after clamp: PL=2 has {run_path_pl2.relative_to(dest).parts}, "
            f"PL=3 has {run_path_pl3.relative_to(dest).parts}"
        )

        # No-clamp verification: without group_modal_depth, PL=3 renders deeper than PL=2.
        unclamped_pl3 = build_dest_path(dest, MBRelease(), MBTrack(), tags_pl3, group_modal_depth=None)
        assert len(unclamped_pl3.relative_to(dest).parts) > len(run_path_pl2.relative_to(dest).parts), (
            "Expected unclamped PL=3 to render deeper than PL=2 (no-clamp baseline)"
        )


class TestRepathExtensionRepair:
    """Tests for repath's extension-less audio file repair path.

    Extension-less files arise when over-long-name truncation ate the audio suffix.  These
    files are invisible to all maintenance passes that gate on an audio suffix.  repath detects
    them via mutagen probing, appends the correct suffix (shortening if the repaired leaf would
    exceed _NAME_MAX), and moves them through the C-PROV provenance chain.
    """

    # Minimal tags for a single-track classical file.  build_dest_path produces:
    #   <dest_root>/Beethoven - Karajan/Symphony No. 5 [rec 2020]/01 - Allegro.flac
    @staticmethod
    def _make_tags() -> TrackTags:
        """Build minimal TrackTags for a single-movement track.

        :returns: A :class:`TrackTags` instance suitable for repath round-trip testing.
        """
        return TrackTags(
            cwp_composer_lastnames="Beethoven",
            cwp_work_top="Symphony No. 5",
            recording_date="2020",
            cwp_movt_num="1",
            movementtotal="1",
            cwp_part_levels="1",
            title="Allegro",
            artist="Karajan",
        )

    @staticmethod
    def _write_journal(dest_root: Path, entries: list[dict[str, str]]) -> None:
        """Write a journal JSON file to ``dest_root / music_annotator_journal.json``.

        :param dest_root: Destination root directory (must already exist).
        :param entries: List of raw entry dicts to serialise.
        """
        journal_path = dest_root / "music_annotator_journal.json"
        journal_path.write_text(json.dumps(entries), encoding="utf-8")

    def test_repath_repairs_extension_less_flac(self, fs: FakeFilesystem) -> None:
        """repath identifies an extension-less FLAC, renames it to .flac, and journals the move.

        An extension-less file that is a valid FLAC (suffix lost during over-long-name
        truncation) must be detected via mutagen probing, renamed with the correct .flac suffix,
        and moved through the C-PROV provenance chain (hash → rename → verify → journal).
        No skip warning is emitted; a "repathed" journal entry records the repair.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags()

        # Create a valid FLAC at the extension-less path (simulates truncation ate the suffix).
        ext_less_path = dest_root / "Beethoven - Karajan" / "Symphony No. 5 [rec 2020]" / "01 - Allegro"
        ext_less_path.parent.mkdir(parents=True, exist_ok=True)
        ext_less_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(ext_less_path, tags)

        # Journal the file as "tagged" at the extension-less path.
        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(ext_less_path),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # The repaired file must exist with the .flac suffix.
        repaired_path = ext_less_path.parent / "01 - Allegro.flac"
        assert repaired_path.exists(), f"Repaired file not found at {repaired_path}"
        assert not ext_less_path.exists(), "Extension-less file should have been renamed"

        # The journal must contain a "repathed" entry for the repair move.
        journal = read_journal(dest_root / JOURNAL_FILENAME)
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) >= 1
        repair_entry = next(e for e in repathed if e.source == str(ext_less_path))
        assert repair_entry.destination == str(repaired_path)

    def test_repath_skips_non_audio_extension_less_file(self, fs: FakeFilesystem) -> None:
        """repath skips an extension-less file that is not a valid audio file.

        When mutagen cannot identify the file as FLAC or MP3, repath logs a "not a track file"
        warning and skips the file — no move is performed and no journal entry is written.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        # Create an extension-less file with non-audio content.
        non_audio_path = dest_root / "Beethoven - Karajan" / "Symphony No. 5 [rec 2020]" / "01 - Allegro"
        non_audio_path.parent.mkdir(parents=True, exist_ok=True)
        non_audio_path.write_bytes(b"this is not audio data at all")

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(non_audio_path),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # The file must remain at its original path (no move).
        assert non_audio_path.exists(), "Non-audio file should not have been moved"

        # No "repathed" journal entry must have been written.
        journal = read_journal(dest_root / JOURNAL_FILENAME)
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 0, f"Expected no repathed entries, got {repathed}"

    def test_repath_repairs_extension_less_flac_over_name_max(self, fs: FakeFilesystem) -> None:
        """repath shortens the repaired leaf when stem + .flac would exceed _NAME_MAX bytes.

        When the extension-less filename is long enough that appending .flac would produce a
        leaf exceeding _NAME_MAX bytes, _proposed_short is applied so that the final repaired
        leaf fits within _NAME_MAX and still ends with .flac.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags()

        # Build a stem that is exactly _NAME_MAX - 2 bytes (so stem + ".flac" = _NAME_MAX + 3,
        # which exceeds the limit and triggers shortening).
        long_stem = "A" * (_NAME_MAX - 2)
        assert len(long_stem.encode("utf-8")) == _NAME_MAX - 2  # noqa: PLR2004
        assert len((long_stem + ".flac").encode("utf-8")) > _NAME_MAX

        ext_less_path = dest_root / "Beethoven - Karajan" / "Symphony No. 5 [rec 2020]" / long_stem
        ext_less_path.parent.mkdir(parents=True, exist_ok=True)
        ext_less_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(ext_less_path, tags)

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(ext_less_path),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # The extension-less file must no longer exist.
        assert not ext_less_path.exists(), "Extension-less file should have been renamed"

        # The repaired file must end with .flac and be within _NAME_MAX bytes.
        repaired_files = list(ext_less_path.parent.glob("*.flac"))
        assert len(repaired_files) >= 1, "Expected at least one .flac file after repair"
        repaired_leaf = repaired_files[0].name
        assert repaired_leaf.endswith(".flac"), f"Repaired leaf must end with .flac, got {repaired_leaf!r}"
        assert len(repaired_leaf.encode("utf-8")) <= _NAME_MAX, (
            f"Repaired leaf {repaired_leaf!r} exceeds _NAME_MAX ({len(repaired_leaf.encode('utf-8'))} > {_NAME_MAX})"
        )

        # A "repathed" journal entry must record the repair.
        journal = read_journal(dest_root / JOURNAL_FILENAME)
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) >= 1
        repair_entry = next(e for e in repathed if e.source == str(ext_less_path))
        assert repair_entry.destination.endswith(".flac")

    def test_repath_repairs_extension_less_mp3(self, fs: FakeFilesystem) -> None:
        """repath identifies an extension-less MP3, renames it to .mp3, and journals the move.

        Covers the MP3 detection branch of _detect_audio_suffix: when mutagen.flac.FLAC fails
        but mutagen.mp3.MP3 succeeds, the correct suffix is ".mp3".

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags()

        # Create a valid MP3 at the extension-less path.
        ext_less_path = dest_root / "Beethoven - Karajan" / "Symphony No. 5 [rec 2020]" / "01 - Allegro"
        ext_less_path.parent.mkdir(parents=True, exist_ok=True)
        ext_less_path.write_bytes(_MINIMAL_MP3)
        apply_tags_mp3(ext_less_path, tags)

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.mp3",
                    "destination": str(ext_less_path),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=False, yes=True)

        # The repaired file must exist with the .mp3 suffix.
        repaired_path = ext_less_path.parent / "01 - Allegro.mp3"
        assert repaired_path.exists(), f"Repaired MP3 file not found at {repaired_path}"
        assert not ext_less_path.exists(), "Extension-less file should have been renamed"

        # A "repathed" journal entry must record the repair.
        journal = read_journal(dest_root / JOURNAL_FILENAME)
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) >= 1
        repair_entry = next(e for e in repathed if e.source == str(ext_less_path))
        assert repair_entry.destination == str(repaired_path)

    def test_repath_extension_repair_dry_run_no_move(self, fs: FakeFilesystem) -> None:
        """repath(dry_run=True) logs the planned repair but does not move or journal the file.

        In dry-run mode, extension-less audio files are identified and the planned repair is
        logged, but no filesystem move is performed and no journal entry is written.

        :param fs: pyfakefs fixture.
        """
        dest_root = Path("/lib")
        fs.create_dir(str(dest_root))

        tags = self._make_tags()

        ext_less_path = dest_root / "Beethoven - Karajan" / "Symphony No. 5 [rec 2020]" / "01 - Allegro"
        ext_less_path.parent.mkdir(parents=True, exist_ok=True)
        ext_less_path.write_bytes(_MINIMAL_FLAC)
        apply_tags_flac(ext_less_path, tags)

        self._write_journal(
            dest_root,
            [
                {
                    "timestamp": "2024-01-01T00:00:00+00:00",
                    "release_id": "r1",
                    "source": "/src/01.flac",
                    "destination": str(ext_less_path),
                    "action": "tagged",
                },
            ],
        )

        music_annotator.repath(dest_root=dest_root, dry_run=True)

        # The extension-less file must remain (no move in dry-run mode).
        assert ext_less_path.exists(), "Extension-less file must not be moved in dry-run mode"
        repaired_path = ext_less_path.parent / "01 - Allegro.flac"
        assert not repaired_path.exists(), "Repaired file must not be created in dry-run mode"

        # No journal entries must have been written.
        journal = read_journal(dest_root / JOURNAL_FILENAME)
        repathed = [e for e in journal.entries if e.action == "repathed"]
        assert len(repathed) == 0, f"Expected no repathed entries in dry-run mode, got {repathed}"
