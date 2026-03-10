"""Unit tests for pipeline functions: build_cea_performers, build_track_tags,
apply_tags_flac, apply_tags_mp3, find_source_files, and run (non-dry-run)."""

from __future__ import annotations

from pathlib import Path

from pyfakefs.fake_filesystem import FakeFilesystem
from pytest_mock import MockerFixture

import music_annotator
from music_annotator import (
    apply_tags_flac,
    apply_tags_mp3,
    build_cea_performers,
    build_track_tags,
    find_source_files,
)
from music_annotator.models import (
    JSON,
    CoverArt,
    MBRecording,
    MBRelease,
    MBTrack,
    MBWork,
    TrackTags,
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
        cover = CoverArt(data=b"\xff\xd8\xff\xe0" + b"\x00" * 100, mime="image/jpeg")
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
# apply_tags_mp3
# ---------------------------------------------------------------------------


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
        cover = CoverArt(data=b"\xff\xd8\xff\xe0" + b"\x00" * 100, mime="image/jpeg")
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
        """Patch all MB API calls.

        :param mocker: pytest-mock fixture.
        :param release: MBRelease model to return from fetch_release.
        """
        mocker.patch("music_annotator.mb.set_useragent")
        mocker.patch("music_annotator.fetch_release", return_value=release)
        mocker.patch("music_annotator.fetch_cover_art", return_value=CoverArt())

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

        mocker.patch("music_annotator.fetch_recording_detail", side_effect=_fetch_rec)
        mocker.patch("music_annotator.fetch_work_detail", return_value=MBWork())

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
        mocker.patch("music_annotator.apply_tags_flac")

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
        mock_tag = mocker.patch("music_annotator.apply_tags_flac")

        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
            fetch_rels=True,
        )
        assert mock_tag.call_count == 2

    def test_tag_error_logged_not_raised(self, mocker: MockerFixture, fs: FakeFilesystem) -> None:
        """A tagging error is logged but does not abort the run.

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
        mocker.patch("music_annotator.apply_tags_flac", side_effect=RuntimeError("tag boom"))

        # Should not raise
        music_annotator.run(
            release_id="rel-1",
            src_dir=src,
            dest_root=dest,
            user_agent="Test/1.0",
            dry_run=False,
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
        mock_cov = mocker.patch("music_annotator.fetch_cover_art", return_value=CoverArt())
        mocker.patch("music_annotator.apply_tags_flac")

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
            "music_annotator.fetch_cover_art",
            return_value=CoverArt(data=jpeg, mime="image/jpeg"),
        )
        mocker.patch("music_annotator.apply_tags_flac")

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

        mocker.patch("music_annotator.mb.set_useragent")
        mocker.patch("music_annotator.fetch_release", return_value=_make_release(n_tracks=1))
        mocker.patch("music_annotator.fetch_cover_art", return_value=CoverArt())
        spy = mocker.patch("music_annotator.fetch_recording_detail")
        mocker.patch("music_annotator.apply_tags_flac")

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
        mock_tag = mocker.patch("music_annotator.apply_tags_mp3")

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
        mocker.patch("music_annotator.MP3", return_value=mock_audio)

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
        mocker.patch("music_annotator.MP3", return_value=mock_audio)

        # Should complete without error — tags is None so delete is never called
        apply_tags_mp3(dest, TrackTags(title="T"))


# ---------------------------------------------------------------------------
# build_track_tags — arranger/orchestrator already in arranger_seen
# ---------------------------------------------------------------------------


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

        mocker.patch("music_annotator.mb.set_useragent")
        mocker.patch("music_annotator.fetch_release", return_value=_make_release(n_tracks=1))
        mocker.patch("music_annotator.fetch_cover_art", return_value=CoverArt())

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

        mocker.patch("music_annotator.fetch_recording_detail", side_effect=_fetch_rec)
        mock_work = mocker.patch(
            "music_annotator.fetch_work_detail",
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
        mocker.patch("music_annotator.apply_tags_flac")

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

        mocker.patch("music_annotator.mb.set_useragent")
        mocker.patch("music_annotator.fetch_release", return_value=_make_release(n_tracks=1))
        mocker.patch("music_annotator.fetch_cover_art", return_value=CoverArt())

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

        mocker.patch("music_annotator.fetch_recording_detail", side_effect=_fetch_rec)
        mock_work = mocker.patch("music_annotator.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator.apply_tags_flac")

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

        mocker.patch("music_annotator.mb.set_useragent")
        mocker.patch("music_annotator.fetch_release", return_value=_make_release(n_tracks=1))
        mocker.patch("music_annotator.fetch_cover_art", return_value=CoverArt())

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

        mocker.patch("music_annotator.fetch_recording_detail", side_effect=_fetch_rec)
        mock_work = mocker.patch("music_annotator.fetch_work_detail", return_value=MBWork())
        mocker.patch("music_annotator.apply_tags_flac")

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
