"""music-annotator — Copy and tag a classical music album using MusicBrainz metadata.

Implements the Classical Extras Picard plugin conventions
(github.com/metabrainz/picard-plugins/tree/2.0/plugins/classical_extras).

Directory layout produced::

    <dest>/
      <Composer lastnames> - <Conductor; Ensemble>/
        <Work title> [YYYY]/
          [nn - <Intermediate division>/]
            nn - <movement title>.<ext>

Tags written (Vorbis Comments for FLAC, ID3v2.4 for MP3):

    Standard Picard tags:
        TITLE, ARTIST, ARTISTS, ARTISTSORT, ALBUMARTIST, ALBUMARTISTSORT,
        ALBUM, TRACKNUMBER, TOTALTRACKS, DISCNUMBER, DATE, ORIGINALDATE,
        COMPOSER, COMPOSERSORT, CONDUCTOR, LYRICIST, ARRANGER, PERFORMER,
        ENSEMBLE, SOLOISTS, BAND, LABEL, ORGANIZATION, CATALOGNUMBER, BARCODE,
        MEDIA, SCRIPT, LANGUAGE, RELEASETYPE, RELEASESTATUS, GENRE,
        WORK, GROUPHEADING, TOP_WORK, PART, MOVEMENT, MOVEMENTNUMBER, MOVEMENTTOTAL,
        KEY, IS_CLASSICAL

    MusicBrainz ID tags (Picard-standard):
        MUSICBRAINZ_ALBUMID, MUSICBRAINZ_TRACKID, MUSICBRAINZ_RECORDINGID,
        MUSICBRAINZ_RELEASEGROUPID, MUSICBRAINZ_ARTISTID, MUSICBRAINZ_ALBUMARTISTID,
        MUSICBRAINZ_WORKID, MUSICBRAINZ_CONDUCTORID, MUSICBRAINZ_COMPOSERID

    Classical Extras _cwp_ variables (stored as tags, prefix CWP_):
        CWP_WORK_0 … CWP_WORK_N, CWP_WORKID_0 … CWP_WORKID_N
        CWP_WORK_0_EN … CWP_WORK_N_EN  (English alias, when available)
        CWP_WORK_0_ALT … CWP_WORK_N_ALT  (unlocaled aliases, semicolon-joined)
        CWP_WORK_TOP, CWP_WORKID_TOP, CWP_WORK_TOP_EN, CWP_WORK_TOP_ALT
        CWP_PART_0 … CWP_PART_N
        CWP_PART_LEVELS, CWP_WORK_PART_LEVELS, CWP_SINGLE_WORK_ALBUM
        CWP_WORK, CWP_GROUPHEADING, CWP_PART, CWP_INTER_WORK
        CWP_MOVT_NUM, CWP_MOVT_TOT
        CWP_COMPOSERS, CWP_COMPOSERS_SORT, CWP_COMPOSER_LASTNAMES
        CWP_WRITERS, CWP_WRITERS_SORT
        CWP_ARRANGERS, CWP_ARRANGERS_SORT, CWP_ARRANGER_NAMES
        CWP_ORCHESTRATORS, CWP_ORCHESTRATORS_SORT
        CWP_RECONSTRUCTORS, CWP_RECONSTRUCTORS_SORT
        CWP_REVISORS, CWP_REVISORS_SORT
        CWP_LYRICISTS, CWP_LYRICISTS_SORT
        CWP_LIBRETTISTS, CWP_LIBRETTISTS_SORT
        CWP_TRANSLATORS, CWP_TRANSLATORS_SORT
        CWP_KEYS, CWP_COMPOSED_DATES, CWP_PUBLISHED_DATES, CWP_PREMIERED_DATES

    Classical Extras _cea_ variables (stored as tags, prefix CEA_):
        CEA_RECORDING_ARTIST, CEA_RECORDING_ARTISTS, CEA_RECORDING_ARTISTS_SORT
        CEA_MB_ARTISTS
        CEA_SOLOISTS, CEA_SOLOIST_NAMES, CEA_SOLOISTS_SORT
        CEA_VOCALISTS, CEA_VOCALIST_NAMES
        CEA_INSTRUMENTALISTS, CEA_INSTRUMENTALIST_NAMES
        CEA_OTHER_SOLOISTS
        CEA_ENSEMBLES, CEA_ENSEMBLE_NAMES, CEA_ENSEMBLES_SORT
        CEA_ALBUM_SOLOISTS, CEA_ALBUM_SOLOISTS_SORT
        CEA_ALBUM_CONDUCTORS, CEA_ALBUM_CONDUCTORS_SORT
        CEA_ALBUM_ENSEMBLES, CEA_ALBUM_ENSEMBLES_SORT
        CEA_ALBUM_COMPOSERS, CEA_ALBUM_COMPOSERS_SORT
        CEA_SUPPORT_PERFORMERS, CEA_SUPPORT_PERFORMERS_SORT
        CEA_CONDUCTORS, CEA_COMPOSERS, CEA_COMPOSER_LASTNAMES, CEA_PERFORMERS
        CEA_ARRANGERS, CEA_ORCHESTRATORS, CEA_CHORUSMASTERS, CEA_LEADERS
        CEA_INSTRUMENTS, CEA_INSTRUMENTS_ALL

    AcoustID tag:
        ACOUSTID_ID

Usage::

    python -m music_annotator \\
        --release-id  53c4d36c-1032-4f78-baba-fc972249d7d1 \\
        --src-dir "/path/to/source/album" \\
        --dest-dir /tmp/music_library \\
        [--user-agent "MyApp/1.0 contact@example.com"] \\
        [--dry-run] [--no-fetch-rels]
"""

from __future__ import annotations

from music_annotator._artists import (
    ARRANGER_RELS,
    CHOIR_STRINGS,
    ENSEMBLE_STRINGS,
    GROUP_STRINGS,
    ORCHESTRA_STRINGS,
    ROLE_ANNOTATIONS,
    artist_credit_phrase,
    artist_ids,
    artist_sort_names,
    is_choir,
    is_ensemble,
    is_orchestra,
    last_name,
)
from music_annotator._console import _console, configure_color
from music_annotator._discover import (
    DiscoverUI,
    TerminalDiscoverUI,
    _format_candidate,
    _parse_release_item,
    _score_toc_release,
    _search_mb_releases,
    _toc_lookup_mb_releases,
    discover,
    parse_dir_hint,
    parse_disc_info_yaml,
    prompt_delete_src,
    prune_sources,
    search_releases_by_dir,
)
from music_annotator._mb_api import (
    _CAA_TYPE_TO_BUCKET,
    _P,
    _SESSION_REL_TYPES,
    _T,
    _WORK_CACHE,
    _cover_art_cache_dir,
    _cover_art_cache_key,
    _extract_session_date,
    _fetch_rg_image,
    _get_bottom_work,
    _infer_mime,
    _mb_call,
    _mb_retry,
    _patched_parse_recording,
    _sidecar_filename,
    fetch_acoustid_id,
    fetch_cover_art,
    fetch_recording_detail,
    fetch_release,
    fetch_work_detail,
    init_mb,
)
from music_annotator._pipeline import (
    CollisionPolicy,
    DiscUI,
    SelectionMethod,
    _apply_collision_suffix,
    _collision_suffix,
    _match_medium_by_title,
    _score_medium_title,
    _select_medium_with_reason,
    _write_freedb_yaml,
    _write_sidecars,
    run,
)
from music_annotator._pipeline_io import (
    _DISC_INFO_FILENAME,
    AUDIO_EXTENSIONS,
    JOURNAL_FILENAME,
    AudioCompareResult,
    _assess_collisions,
    _check_collisions,
    _parse_disc_id_list,
    _preferred_disc_record,
    _read_acoustid_tag,
    _read_duration_ms,
    _read_tags_flac,
    _read_tags_mp3,
    _run_fpcalc,
    _sha256_file,
    _verify_copy,
    compare_audio_collision,
    find_source_files,
    parse_disc_title,
    parse_disc_toc,
    read_journal,
    write_transaction_log,
)
from music_annotator._tagger import (
    _FLAC_MAX_PICTURE_BYTES,
    _MP3_STD_KEYS,
    _MP3_TXXX_MAP,
    apply_tags_flac,
    apply_tags_mp3,
)
from music_annotator._tags import (
    _SAFE_RE,
    _rec_title,
    _work_aliases,
    build_cea_performers,
    build_cwp_tags,
    build_dest_path,
    build_track_tags,
    safe_name,
)
from music_annotator._works import (
    PERIOD_MAP,
    WORKTYPE_GENRES,
    _date_range,
    build_work_hierarchy,
    collect_work_dates,
    collect_work_tags_and_key,
    collect_work_urls,
    extract_work_artist_rels,
    parse_year,
    period_for_year,
    strip_common_prefix,
)
from music_annotator.models import CopyPlanEntry, DirHint, PeriodEntry, PictureEntry

__all__ = [
    "configure_color",
    "init_mb",
    "fetch_release",
    "fetch_recording_detail",
    "fetch_cover_art",
    "fetch_work_detail",
    "fetch_acoustid_id",
    "is_ensemble",
    "is_choir",
    "is_orchestra",
    "artist_credit_phrase",
    "artist_ids",
    "artist_sort_names",
    "last_name",
    "build_work_hierarchy",
    "strip_common_prefix",
    "period_for_year",
    "extract_work_artist_rels",
    "collect_work_dates",
    "collect_work_urls",
    "parse_year",
    "collect_work_tags_and_key",
    "build_cea_performers",
    "build_cwp_tags",
    "build_track_tags",
    "safe_name",
    "build_dest_path",
    "apply_tags_flac",
    "apply_tags_mp3",
    "find_source_files",
    "_sha256_file",
    "_read_tags_flac",
    "_read_tags_mp3",
    "_verify_copy",
    "CollisionPolicy",
    "SelectionMethod",
    "run",
    "parse_disc_info_yaml",
    "parse_disc_title",
    "parse_disc_toc",
    "parse_dir_hint",
    "search_releases_by_dir",
    "discover",
    "prompt_delete_src",
    "prune_sources",
    "read_journal",
    "write_transaction_log",
    "JOURNAL_FILENAME",
]

# Suppress "imported but unused" for items re-exported but not listed in __all__
# (used by tests or __main__ via `import music_annotator; music_annotator.X`)
_reexports = (
    ARRANGER_RELS,
    AUDIO_EXTENSIONS,
    CHOIR_STRINGS,
    DiscoverUI,
    ENSEMBLE_STRINGS,
    GROUP_STRINGS,
    ORCHESTRA_STRINGS,
    PERIOD_MAP,
    ROLE_ANNOTATIONS,
    TerminalDiscoverUI,
    WORKTYPE_GENRES,
    _FLAC_MAX_PICTURE_BYTES,
    _SAFE_RE,
    _MP3_STD_KEYS,
    _MP3_TXXX_MAP,
    _P,
    _T,
    _WORK_CACHE,
    _DISC_INFO_FILENAME,
    AudioCompareResult,
    _apply_collision_suffix,
    _assess_collisions,
    _check_collisions,
    _collision_suffix,
    _console,
    _cover_art_cache_dir,
    _cover_art_cache_key,
    _fetch_rg_image,
    _parse_disc_id_list,
    _preferred_disc_record,
    _read_acoustid_tag,
    _read_duration_ms,
    _run_fpcalc,
    compare_audio_collision,
    _format_candidate,
    _get_bottom_work,
    _extract_session_date,
    _infer_mime,
    _mb_call,
    _mb_retry,
    _patched_parse_recording,
    _parse_release_item,
    _SESSION_REL_TYPES,
    _sidecar_filename,
    _date_range,
    collect_work_urls,
    CopyPlanEntry,
    DirHint,
    PeriodEntry,
    PictureEntry,
    _rec_title,
    _score_medium_title,
    _score_toc_release,
    _search_mb_releases,
    _select_medium_with_reason,
    _toc_lookup_mb_releases,
    _write_freedb_yaml,
    _write_sidecars,
    _work_aliases,
    DiscUI,
    SelectionMethod,
    _match_medium_by_title,
    parse_disc_title,
)
