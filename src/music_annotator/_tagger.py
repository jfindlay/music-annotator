"""FLAC and MP3 audio tagging functions for music-annotator.

Provides :func:`apply_tags_flac` and :func:`apply_tags_mp3` which write the full set of
Classical Extras tags to audio files using mutagen, and the helper tables :data:`_MP3_STD_KEYS`
and :data:`_MP3_TXXX_MAP` that are shared with the verification functions in
:mod:`music_annotator._pipeline_io`.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from mutagen._util import MutagenError
from mutagen.flac import FLAC
from mutagen.flac import Picture as FLACPicture
from mutagen.id3 import (  # type: ignore[attr-defined]
    APIC,
    ID3,
    TALB,
    TCOM,
    TDOR,
    TDRC,
    TIT2,
    TLEN,
    TPE1,
    TPE2,
    TPE3,
    TPOS,
    TPUB,
    TRCK,
    TSRC,
    TSST,
    TXXX,
)
from mutagen.mp3 import MP3

from music_annotator.models import CoverArt, TrackTags

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: Standard ID3 text-frame keys written by :func:`apply_tags_mp3` (excluding ``TRACKNUMBER`` which uses special
#: ``N/total`` formatting handled separately).
_MP3_STD_KEYS: frozenset[str] = frozenset(
    {
        "TITLE",
        "ARTIST",
        "ALBUMARTIST",
        "ALBUM",
        "TRACKNUMBER",
        "DISCNUMBER",
        "DATE",
        "ORIGINALDATE",
        "COMPOSER",
        "CONDUCTOR",
        "ORGANIZATION",
        "ISRC",
        "LENGTH",
        "DISCSUBTITLE",
    }
)

#: Mapping from uppercase tag-dict key to TXXX frame description string, used by both :func:`apply_tags_mp3` and
#: :func:`_read_tags_mp3` so that the same table drives both writing and read-back verification.
_MP3_TXXX_MAP: dict[str, str] = {
    "MUSICBRAINZ_ALBUMID": "MusicBrainz Album Id",
    "MUSICBRAINZ_RECORDINGID": "MusicBrainz Track Id",
    "MUSICBRAINZ_RELEASEGROUPID": "MusicBrainz Release Group Id",
    "MUSICBRAINZ_ALBUMARTISTID": "MusicBrainz Album Artist Id",
    "MUSICBRAINZ_ARTISTID": "MusicBrainz Artist Id",
    "MUSICBRAINZ_WORKID": "MusicBrainz Work Id",
    "MUSICBRAINZ_CONDUCTORID": "MusicBrainz Conductor Id",
    "MUSICBRAINZ_COMPOSERID": "MusicBrainz Composer Id",
    "CATALOGNUMBER": "CATALOGNUMBER",
    "BARCODE": "BARCODE",
    "WORK": "WORK",
    "GROUPHEADING": "GROUPHEADING",
    "TOP_WORK": "TOP_WORK",
    "PART": "PART",
    "MOVEMENT": "MOVEMENT",
    "MOVEMENTNUMBER": "MOVEMENTNUMBER",
    "MOVEMENTTOTAL": "MOVEMENTTOTAL",
    "IS_CLASSICAL": "IS_CLASSICAL",
    "GENRE": "GENRE",
    "PERIOD": "PERIOD",
    "KEY": "KEY",
    "WORK_YEAR": "WORK_YEAR",
    "COMPOSED_DATE": "COMPOSED_DATE",
    "LANGUAGE": "LANGUAGE",
    "SCRIPT": "SCRIPT",
    "RELEASETYPE": "MusicBrainz Album Type",
    "RELEASESTATUS": "MusicBrainz Album Status",
    "SOLOISTS": "SOLOISTS",
    "ENSEMBLE": "ENSEMBLE",
    "BAND": "BAND",
    "VOCALISTS": "VOCALISTS",
    "INSTRUMENTALISTS": "INSTRUMENTALISTS",
    "INSTRUMENT": "INSTRUMENT",
    "LYRICIST": "LYRICIST",
    "TRANSLATOR": "TRANSLATOR",
    "ARRANGER": "ARRANGER",
    "CHORUSMASTER": "CHORUSMASTER",
    "LEADER": "LEADER",
    "PRODUCER": "PRODUCER",
    "ENGINEER": "ENGINEER",
    "TOTALTRACKS": "TOTALTRACKS",
    "ARTISTS": "ARTISTS",
    "ARTISTSORT": "ARTISTSORT",
    "ALBUMARTISTSORT": "ALBUMARTISTSORT",
    "COMPOSERSORT": "COMPOSERSORT",
    "SUBTITLE": "SUBTITLE",
    "PUBLISHED_DATE": "PUBLISHED_DATE",
    "PREMIERED_DATE": "PREMIERED_DATE",
    "MUSICBRAINZ_RELEASETRACKID": "MusicBrainz Release Track Id",
    "ACOUSTID_ID": "Acoustid Id",
    "AUDIO_HASH": "Audio Hash",
    "CHROMAPRINT_FP": "Chromaprint Fingerprint",
    # AccurateRip per-track fields (C-AR).  desc == key (own-namespace convention).
    "ACCURATERIP_V1_RESULT": "ACCURATERIP_V1_RESULT",
    "ACCURATERIP_V1_CONFIDENCE": "ACCURATERIP_V1_CONFIDENCE",
    "ACCURATERIP_V1_LOCAL_CRC": "ACCURATERIP_V1_LOCAL_CRC",
    "ACCURATERIP_V1_REMOTE_CRC": "ACCURATERIP_V1_REMOTE_CRC",
    "ACCURATERIP_V2_RESULT": "ACCURATERIP_V2_RESULT",
    "ACCURATERIP_V2_CONFIDENCE": "ACCURATERIP_V2_CONFIDENCE",
    "ACCURATERIP_V2_LOCAL_CRC": "ACCURATERIP_V2_LOCAL_CRC",
    "ACCURATERIP_V2_REMOTE_CRC": "ACCURATERIP_V2_REMOTE_CRC",
    "ACCURATERIP_TEST_CRC": "ACCURATERIP_TEST_CRC",
    "ACCURATERIP_COPY_CRC": "ACCURATERIP_COPY_CRC",
    "ACCURATERIP_STATUS": "ACCURATERIP_STATUS",
    # CEA tags
    "CEA_RECORDING_ARTIST": "CEA_RECORDING_ARTIST",
    "CEA_RECORDING_ARTISTS": "CEA_RECORDING_ARTISTS",
    "CEA_RECORDING_ARTISTS_SORT": "CEA_RECORDING_ARTISTS_SORT",
    "CEA_MB_ARTISTS": "CEA_MB_ARTISTS",
    "CEA_SOLOISTS": "CEA_SOLOISTS",
    "CEA_SOLOIST_NAMES": "CEA_SOLOIST_NAMES",
    "CEA_SOLOISTS_SORT": "CEA_SOLOISTS_SORT",
    "CEA_VOCALISTS": "CEA_VOCALISTS",
    "CEA_VOCALIST_NAMES": "CEA_VOCALIST_NAMES",
    "CEA_INSTRUMENTALISTS": "CEA_INSTRUMENTALISTS",
    "CEA_INSTRUMENTALIST_NAMES": "CEA_INSTRUMENTALIST_NAMES",
    "CEA_OTHER_SOLOISTS": "CEA_OTHER_SOLOISTS",
    "CEA_ENSEMBLES": "CEA_ENSEMBLES",
    "CEA_ENSEMBLE_NAMES": "CEA_ENSEMBLE_NAMES",
    "CEA_ENSEMBLES_SORT": "CEA_ENSEMBLES_SORT",
    "CEA_ALBUM_SOLOISTS": "CEA_ALBUM_SOLOISTS",
    "CEA_ALBUM_SOLOISTS_SORT": "CEA_ALBUM_SOLOISTS_SORT",
    "CEA_ALBUM_CONDUCTORS": "CEA_ALBUM_CONDUCTORS",
    "CEA_ALBUM_CONDUCTORS_SORT": "CEA_ALBUM_CONDUCTORS_SORT",
    "CEA_ALBUM_ENSEMBLES": "CEA_ALBUM_ENSEMBLES",
    "CEA_ALBUM_ENSEMBLES_SORT": "CEA_ALBUM_ENSEMBLES_SORT",
    "CEA_ALBUM_COMPOSERS": "CEA_ALBUM_COMPOSERS",
    "CEA_ALBUM_COMPOSERS_SORT": "CEA_ALBUM_COMPOSERS_SORT",
    "CEA_SUPPORT_PERFORMERS": "CEA_SUPPORT_PERFORMERS",
    "CEA_SUPPORT_PERFORMERS_SORT": "CEA_SUPPORT_PERFORMERS_SORT",
    "CEA_CONDUCTORS": "CEA_CONDUCTORS",
    "CEA_COMPOSERS": "CEA_COMPOSERS",
    "CEA_COMPOSER_LASTNAMES": "CEA_COMPOSER_LASTNAMES",
    "CEA_PERFORMERS": "CEA_PERFORMERS",
    "CEA_ARRANGERS": "CEA_ARRANGERS",
    "CEA_ORCHESTRATORS": "CEA_ORCHESTRATORS",
    "CEA_CHORUSMASTERS": "CEA_CHORUSMASTERS",
    "CEA_LEADERS": "CEA_LEADERS",
    "CEA_INSTRUMENTS": "CEA_INSTRUMENTS",
    "CEA_INSTRUMENTS_ALL": "CEA_INSTRUMENTS_ALL",
    # CWP tags
    "CWP_WORK_TOP": "CWP_WORK_TOP",
    "CWP_WORKID_TOP": "CWP_WORKID_TOP",
    "CWP_PART_LEVELS": "CWP_PART_LEVELS",
    "CWP_WORK_PART_LEVELS": "CWP_WORK_PART_LEVELS",
    "CWP_GROUPHEADING": "CWP_GROUPHEADING",
    "CWP_PART": "CWP_PART",
    "CWP_WORK": "CWP_WORK",
    "CWP_INTER_WORK": "CWP_INTER_WORK",
    "CWP_MOVT_NUM": "CWP_MOVT_NUM",
    "CWP_MOVT_TOT": "CWP_MOVT_TOT",
    "CWP_SINGLE_WORK_ALBUM": "CWP_SINGLE_WORK_ALBUM",
    "CWP_COMPOSERS": "CWP_COMPOSERS",
    "CWP_COMPOSERS_SORT": "CWP_COMPOSERS_SORT",
    "CWP_COMPOSER_LASTNAMES": "CWP_COMPOSER_LASTNAMES",
    "CWP_WRITERS": "CWP_WRITERS",
    "CWP_WRITERS_SORT": "CWP_WRITERS_SORT",
    "CWP_ARRANGERS": "CWP_ARRANGERS",
    "CWP_ARRANGERS_SORT": "CWP_ARRANGERS_SORT",
    "CWP_ARRANGER_NAMES": "CWP_ARRANGER_NAMES",
    "CWP_ORCHESTRATORS": "CWP_ORCHESTRATORS",
    "CWP_ORCHESTRATORS_SORT": "CWP_ORCHESTRATORS_SORT",
    "CWP_RECONSTRUCTORS": "CWP_RECONSTRUCTORS",
    "CWP_RECONSTRUCTORS_SORT": "CWP_RECONSTRUCTORS_SORT",
    "CWP_REVISORS": "CWP_REVISORS",
    "CWP_REVISORS_SORT": "CWP_REVISORS_SORT",
    "CWP_LYRICISTS": "CWP_LYRICISTS",
    "CWP_LYRICISTS_SORT": "CWP_LYRICISTS_SORT",
    "CWP_LIBRETTISTS": "CWP_LIBRETTISTS",
    "CWP_LIBRETTISTS_SORT": "CWP_LIBRETTISTS_SORT",
    "CWP_TRANSLATORS": "CWP_TRANSLATORS",
    "CWP_TRANSLATORS_SORT": "CWP_TRANSLATORS_SORT",
    "CWP_KEYS": "CWP_KEYS",
    "CWP_COMPOSED_DATES": "CWP_COMPOSED_DATES",
    "CWP_PUBLISHED_DATES": "CWP_PUBLISHED_DATES",
    "CWP_PREMIERED_DATES": "CWP_PREMIERED_DATES",
    "CWP_WORKTYPE_GENRES": "CWP_WORKTYPE_GENRES",
    # Cover art sidecar file references (all 18 CAA types)
    "COVERART_FRONT_FILE": "COVERART_FRONT_FILE",
    "COVERART_BACK_FILE": "COVERART_BACK_FILE",
    "COVERART_BOOKLET_FILES": "COVERART_BOOKLET_FILES",
    "COVERART_MEDIUM_FILES": "COVERART_MEDIUM_FILES",
    "COVERART_TRAY_FILES": "COVERART_TRAY_FILES",
    "COVERART_OBI_FILES": "COVERART_OBI_FILES",
    "COVERART_SPINE_FILES": "COVERART_SPINE_FILES",
    "COVERART_TRACK_FILES": "COVERART_TRACK_FILES",
    "COVERART_LINER_FILES": "COVERART_LINER_FILES",
    "COVERART_STICKER_FILES": "COVERART_STICKER_FILES",
    "COVERART_POSTER_FILES": "COVERART_POSTER_FILES",
    "COVERART_MATRIX_FILES": "COVERART_MATRIX_FILES",
    "COVERART_TOP_FILES": "COVERART_TOP_FILES",
    "COVERART_BOTTOM_FILES": "COVERART_BOTTOM_FILES",
    "COVERART_PANEL_FILES": "COVERART_PANEL_FILES",
    "COVERART_WATERMARK_FILES": "COVERART_WATERMARK_FILES",
    "COVERART_RAW_FILES": "COVERART_RAW_FILES",
    "COVERART_OTHER_FILES": "COVERART_OTHER_FILES",
    # Standard Picard fields added from MB data
    "RELEASECOUNTRY": "RELEASECOUNTRY",
    "TOTALDISCS": "TOTALDISCS",
    "RELEASETYPE_SECONDARY": "RELEASETYPE_SECONDARY",
    "PACKAGING": "PACKAGING",
    "ASIN": "ASIN",
    "LABEL_CODE": "LABEL_CODE",
    "MUSICBRAINZ_LABELID": "MusicBrainz Label Id",
    "COMMENT": "COMMENT",
    "RELEASEDISAMBIGUATION": "RELEASEDISAMBIGUATION",
    "RECORDING_DATE": "RECORDING_DATE",
    "ISWC": "ISWC",
    "WORK_DISAMBIGUATION": "WORK_DISAMBIGUATION",
    "WORK_ANNOTATION": "WORK_ANNOTATION",
    "WORK_IMSLP_URL": "WORK_IMSLP_URL",
    "WORK_WIKIDATA_URL": "WORK_WIKIDATA_URL",
    "MUSICBRAINZ_SERIES": "MUSICBRAINZ_SERIES",
    "CAA_FRONT": "CAA_FRONT",
    "CAA_BACK": "CAA_BACK",
    "CEA_PERFORMERS_CREDITED": "CEA_PERFORMERS_CREDITED",
}

#: Maximum bytes for a single FLAC metadata block (24-bit unsigned = 2^24 - 1 ≈ 16.7 MB).
#: Used as a guard before embedding images; 500 px JPEGs are well under this limit.
_FLAC_MAX_PICTURE_BYTES: int = 16_000_000


def apply_tags_flac(dest_file: Path, tags: TrackTags, cover: CoverArt | None = None) -> None:
    """Write Vorbis Comment tags and all available cover art pictures to a FLAC file.

    Clears any existing tags and PICTURE blocks, writes all non-internal non-empty fields from ``tags`` as lowercase
    Vorbis Comment keys, then embeds every image in ``cover`` as a FLAC PICTURE block using the appropriate
    ``PictureType`` value for each CAA image category:

    - ``front`` images → ``PictureType.COVER_FRONT`` (3)
    - ``back`` images → ``PictureType.COVER_BACK`` (4)
    - ``booklet`` images → ``PictureType.LEAFLET_PAGE`` (5)
    - ``medium`` images → ``PictureType.MEDIA`` (6)

    All images within a category are embedded in listing order.  When multiple images share the same ``PictureType``,
    each gets a unique ``desc`` suffixed with its 1-based index (e.g. ``"Booklet 1"``, ``"Booklet 2"``).

    :param dest_file: Path to the destination FLAC file (must already exist).
    :param tags: The :class:`~music_annotator.models.TrackTags` instance to write.
    :param cover: Optional :class:`~music_annotator.models.CoverArt`; all available images are embedded when provided.
    :raises mutagen.MutagenError: If the file cannot be read or written.
    """
    audio = FLAC(str(dest_file))
    audio.clear()
    for key, value in tags.to_file_dict().items():
        audio[key.lower()] = value

    # Embed only the 500 px front cover image (COVER_FRONT = type 3).
    # All other images (back, booklet, medium, original-resolution front) are written as
    # sidecar files by _pipeline._write_sidecars and are not embedded in audio files.
    if cover and cover.front:
        for idx, img in enumerate(cover.front, start=1):
            if len(img.data) > _FLAC_MAX_PICTURE_BYTES:
                log.warning("cover_art_too_large_to_embed", size=len(img.data), limit=_FLAC_MAX_PICTURE_BYTES)
                continue
            pic = FLACPicture()  # type: ignore[no-untyped-call]
            pic.type = 3  # COVER_FRONT
            pic.mime = img.mime or "image/jpeg"
            pic.desc = "Cover" if len(cover.front) == 1 else f"Cover {idx}"
            pic.width = pic.height = pic.depth = pic.colors = 0
            pic.data = img.data
            audio.add_picture(pic)  # type: ignore[no-untyped-call]

    audio.save()
    log.debug("tagged_flac", path=str(dest_file))


def apply_tags_mp3(dest_file: Path, tags: TrackTags, cover: CoverArt | None = None) -> None:
    """Write ID3v2.4 tags and all available cover art pictures to an MP3 file.

    Deletes any existing ID3 tags, writes standard text frames (``TIT2``, ``TPE1``, etc.) and ``TXXX`` frames for all
    non-internal non-empty fields, then embeds every image in ``cover`` as an ``APIC`` frame using the appropriate
    ID3 picture type for each CAA image category:

    - ``front`` images → APIC type 3 (``COVER_FRONT``)
    - ``back`` images → APIC type 4 (``COVER_BACK``)
    - ``booklet`` images → APIC type 5 (``LEAFLET_PAGE``)
    - ``medium`` images → APIC type 6 (``MEDIA``)

    All images within a category are embedded in listing order.  When multiple images share the same APIC type, each
    gets a unique ``desc`` suffixed with its 1-based index (e.g. ``"Booklet 1"``, ``"Booklet 2"``) so that ID3 frames,
    which are keyed by ``(type, desc)``, remain distinct.

    :param dest_file: Path to the destination MP3 file (must already exist).
    :param tags: The :class:`~music_annotator.models.TrackTags` instance to write.
    :param cover: Optional :class:`~music_annotator.models.CoverArt`; all available images are embedded when provided.
    :raises mutagen.MutagenError: If the file cannot be read or written.
    """
    try:
        audio = MP3(str(dest_file))
        if audio.tags:
            audio.tags.delete(str(dest_file))
    except (MutagenError, OSError):
        pass

    id3_tags = ID3()  # type: ignore[no-untyped-call]
    file_dict = tags.to_file_dict()

    def txxx(desc: str, val: str) -> None:
        if val:
            id3_tags.add(TXXX(encoding=3, desc=desc, text=val))  # type: ignore[no-untyped-call]

    if file_dict.get("TITLE"):
        id3_tags.add(TIT2(encoding=3, text=file_dict["TITLE"]))  # type: ignore[no-untyped-call]
    if file_dict.get("ARTIST"):
        id3_tags.add(TPE1(encoding=3, text=file_dict["ARTIST"]))  # type: ignore[no-untyped-call]
    if file_dict.get("ALBUMARTIST"):
        id3_tags.add(TPE2(encoding=3, text=file_dict["ALBUMARTIST"]))  # type: ignore[no-untyped-call]
    if file_dict.get("ALBUM"):
        id3_tags.add(TALB(encoding=3, text=file_dict["ALBUM"]))  # type: ignore[no-untyped-call]
    if file_dict.get("TRACKNUMBER"):
        total = file_dict.get("TOTALTRACKS", "")
        trck_text = f"{file_dict['TRACKNUMBER']}/{total}" if total else file_dict["TRACKNUMBER"]
        id3_tags.add(TRCK(encoding=3, text=trck_text))  # type: ignore[no-untyped-call]
    if file_dict.get("DISCNUMBER"):
        id3_tags.add(TPOS(encoding=3, text=file_dict["DISCNUMBER"]))  # type: ignore[no-untyped-call]
    if file_dict.get("DATE"):
        id3_tags.add(TDRC(encoding=3, text=file_dict["DATE"]))  # type: ignore[no-untyped-call]
    if file_dict.get("ORIGINALDATE"):
        id3_tags.add(TDOR(encoding=3, text=file_dict["ORIGINALDATE"]))  # type: ignore[no-untyped-call]
    if file_dict.get("COMPOSER"):
        id3_tags.add(TCOM(encoding=3, text=file_dict["COMPOSER"]))  # type: ignore[no-untyped-call]
    if file_dict.get("CONDUCTOR"):
        id3_tags.add(TPE3(encoding=3, text=file_dict["CONDUCTOR"]))  # type: ignore[no-untyped-call]
    if file_dict.get("ORGANIZATION"):
        id3_tags.add(TPUB(encoding=3, text=file_dict["ORGANIZATION"]))  # type: ignore[no-untyped-call]
    if file_dict.get("ISRC"):
        # ISRC: write first ISRC as a dedicated TSRC frame; additional ISRCs go to TXXX.
        isrc_vals = file_dict["ISRC"].split("; ")
        id3_tags.add(TSRC(encoding=3, text=isrc_vals[0]))  # type: ignore[no-untyped-call]
    if file_dict.get("LENGTH"):
        id3_tags.add(TLEN(encoding=3, text=file_dict["LENGTH"]))  # type: ignore[no-untyped-call]
    if file_dict.get("DISCSUBTITLE"):
        id3_tags.add(TSST(encoding=3, text=file_dict["DISCSUBTITLE"]))  # type: ignore[no-untyped-call]

    for meta_key, txxx_desc in _MP3_TXXX_MAP.items():
        txxx(txxx_desc, file_dict.get(meta_key, ""))

    # Embed only the 500 px front cover image (APIC type 3 = COVER_FRONT).
    if cover and cover.front:
        for idx, img in enumerate(cover.front, start=1):
            id3_tags.add(  # type: ignore[no-untyped-call]
                APIC(  # type: ignore[no-untyped-call]
                    encoding=3,
                    mime=img.mime or "image/jpeg",
                    type=3,
                    desc="Cover" if len(cover.front) == 1 else f"Cover {idx}",
                    data=img.data,
                )
            )

    id3_tags.save(str(dest_file), v2_version=4)
    log.debug("tagged_mp3", path=str(dest_file))
