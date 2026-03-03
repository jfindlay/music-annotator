#!/usr/bin/env python3
"""
annotate_mb.py — Copy a music album directory to a destination tree, tagging
every FLAC/MP3 file with MusicBrainz metadata following the Classical Extras
plugin conventions (github.com/metabrainz/picard-plugins/tree/2.0/plugins/classical_extras).

Directory layout produced:
  <dest>/
    <Composer sort-name> - <Performers>/
      <Work title> (<work MBID>)/
        <nn> - <movement title>.<ext>

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

  Classical Extras style _cwp_ variables (stored as tags, prefix cwp_):
    cwp_work_0 … cwp_work_N   — MB work name at each hierarchy level
    cwp_workid_0 … cwp_workid_N
    cwp_work_top, cwp_workid_top
    cwp_part_0 … cwp_part_N   — stripped movement/part name at each level
    cwp_part_levels            — number of work hierarchy levels
    cwp_work_part_levels       — max levels for top work on this release
    cwp_single_work_album      — 1 if album is one top-work, else 0
    cwp_work, cwp_groupheading — selected single/multi-level work names
    cwp_part, cwp_inter_work   — movement name and intermediate works
    cwp_movt_num, cwp_movt_tot
    cwp_composers, cwp_composers_sort, cwp_composer_lastnames
    cwp_arrangers, cwp_orchestrators, cwp_reconstructors, cwp_revisors
    cwp_lyricists, cwp_librettists, cwp_translators
    cwp_keys, cwp_composed_dates, cwp_published_dates, cwp_premiered_dates

  Classical Extras style _cea_ variables (stored as tags, prefix cea_):
    cea_recording_artist, cea_recording_artists
    cea_soloists, cea_soloist_names
    cea_vocalists, cea_instrumentalists, cea_other_soloists
    cea_ensembles, cea_ensemble_names
    cea_conductors, cea_composers, cea_composer_lastnames, cea_performers
    cea_arrangers, cea_orchestrators, cea_chorusmasters, cea_leaders
    cea_instruments, cea_instruments_credited

Usage:
  python annotate_mb.py \\
      --release-id  53c4d36c-1032-4f78-baba-fc972249d7d1 \\
      --src-dir "/path/to/source/album" \\
      --dest-dir /tmp/music_library \\
      [--user-agent "MyApp/1.0 contact@example.com"] \\
      [--dry-run] [--no-fetch-rels]
"""

import argparse
import os
import re
import shutil
import sys
import time
from pathlib import Path
from collections import defaultdict

import musicbrainzngs as mb
from mutagen.flac import FLAC, Picture as FLACPicture
from mutagen.id3 import (
    ID3, TIT2, TPE1, TPE2, TALB, TRCK, TPOS, TDRC, TDOR,
    TCOM, TPE3, TPUB, TXXX, TOAL, APIC, error as ID3Error,
)
from mutagen.id3 import PictureType as ID3PictureType
from mutagen.mp3 import MP3

# ---------------------------------------------------------------------------
# Constants mirroring Classical Extras defaults
# ---------------------------------------------------------------------------

# Strings that identify ensemble-type performers (from CEA_ORCHESTRAS / CEA_CHOIRS / CEA_GROUPS)
ORCHESTRA_STRINGS = {
    'orchestra', 'philharmonic', 'philharmonica', 'philharmoniker',
    'musicians', 'academy', 'symphony', 'orkester',
}
CHOIR_STRINGS = {
    'choir', 'chorus', 'singers', 'domchor', 'koor', 'kammerkoor',
}
GROUP_STRINGS = {
    'ensemble', 'band', 'trio', 'quartet', 'quintet', 'sextet',
    'septet', 'octet', 'chamber', 'consort', 'players', 'quartett',
}
ENSEMBLE_STRINGS = ORCHESTRA_STRINGS | CHOIR_STRINGS | GROUP_STRINGS

# Annotation labels for specialist roles (cea_* annotation defaults)
ROLE_ANNOTATIONS = {
    'arranger': 'arr.',
    'instrument arranger': 'arr.',
    'vocal arranger': 'arr.',
    'orchestrator': 'orch.',
    'reconstructed by': 'reconstructed',
    'revised by': 'revised',
    'translator': 'trans.',
    'lyricist': 'lyrics',
    'librettist': 'libretto',
    'writer': 'writer',
    'chorus master': 'choirmaster',
    'concertmaster': 'leader',
    'balance': 'balance',
    'producer': 'producer',
}

# Relationship types that go into the ARRANGER tag (CE convention)
ARRANGER_RELS = {'arranger', 'instrument arranger', 'vocal arranger', 'orchestrator', 'reconstructed by', 'revised by'}

# Classical Extras period map (default)
PERIOD_MAP = [
    ('Early',         -3000, 800),
    ('Medieval',        800, 1400),
    ('Renaissance',    1400, 1600),
    ('Baroque',        1600, 1750),
    ('Classical',      1750, 1820),
    ('Early Romantic', 1800, 1850),
    ('Late Romantic',  1850, 1910),
    ('20th Century',   1910, 1975),
    ('Contemporary',   1975, 2525),
]

# Work type → genre (CE worktype_genres logic)
WORKTYPE_GENRES = {
    'Symphony': 'Symphony',
    'Concerto': 'Concerto',
    'Opera': 'Opera',
    'Oratorio': 'Oratorio',
    'Cantata': 'Cantata',
    'Mass': 'Mass',
    'Motet': 'Motet',
    'Ballet': 'Ballet',
    'Symphonic poem': 'Symphonic poem',
    'Suite': 'Suite',
    'Overture': 'Overture',
    'Chamber music': 'Chamber music',
    'Sonata': 'Sonata',
    'Song cycle': 'Song-cycle',
    'Choral': 'Choral',
    'Partita': 'Partita',
    'Aria': 'Aria',
}

# ---------------------------------------------------------------------------
# MusicBrainz helpers
# ---------------------------------------------------------------------------

def init_mb(user_agent: str):
    parts = user_agent.split('/', 1)
    app = parts[0].strip()
    rest = parts[1].strip() if len(parts) > 1 else '1.0'
    vc = rest.split(None, 1)
    version = vc[0]
    contact = vc[1] if len(vc) > 1 else ''
    mb.set_useragent(app, version, contact)


def _mb_get(fn, *args, **kwargs):
    """Call a musicbrainzngs function with simple exponential-backoff retry."""
    for attempt in range(6):
        try:
            return fn(*args, **kwargs)
        except mb.ResponseError as exc:
            code = str(exc)
            if '503' in code or '429' in code or '500' in code:
                wait = 2 ** attempt
                print(f'    MB rate-limit ({code[:20]}…), waiting {wait}s …', file=sys.stderr)
                time.sleep(wait)
            else:
                raise
    raise RuntimeError(f'MB request failed after retries: {fn.__name__}')


def fetch_release(release_id: str) -> dict:
    result = _mb_get(
        mb.get_release_by_id, release_id,
        includes=[
            'artists', 'recordings', 'release-groups', 'labels', 'media',
            'artist-credits', 'work-rels', 'recording-level-rels',
        ],
    )
    time.sleep(1)
    return result['release']


def fetch_recording_detail(recording_id: str) -> dict:
    """Fetch full recording with artist-rels, work-rels, work-level-rels."""
    result = _mb_get(
        mb.get_recording_by_id, recording_id,
        includes=['artists', 'work-rels', 'artist-rels'],
    )
    time.sleep(1)
    return result.get('recording', {})


_WORK_CACHE: dict = {}   # work_id → work dict, avoids redundant MB API calls


def fetch_cover_art(release_id: str, release_group_id: str = '') -> tuple[bytes, str]:
    """
    Download the front cover art for a release.

    Strategy:
      1. Try the release's own CAA entry via mb.get_image_front().
      2. On 404 (no art for this release), try the release-group front via
         mb.get_release_group_image_front().
      3. On any other error, return (b'', '').

    Returns (image_bytes, mime_type).  mime_type is inferred from the first
    four bytes of the image (JPEG magic FF D8, PNG magic 89 50 4E 47).
    """
    def _mime(data: bytes) -> str:
        if data[:2] == b'\xff\xd8':
            return 'image/jpeg'
        if data[:4] == b'\x89PNG':
            return 'image/png'
        return 'image/jpeg'   # safe default for CAA which almost always serves JPEG

    # 1. Release-level front cover (size=500 is a good balance)
    try:
        print('  Fetching cover art from Cover Art Archive …', flush=True)
        data = mb.get_image_front(release_id, size='500')
        time.sleep(1)
        if data:
            return bytes(data), _mime(bytes(data))
    except mb.ResponseError as exc:
        code = str(exc)
        if '404' in code:
            print('    Release has no CAA entry, trying release-group …', file=sys.stderr)
        else:
            print(f'    CAA release error ({code[:40]}), trying release-group …', file=sys.stderr)

    # 2. Release-group front cover (covers parent release group, often present
    #    even when the specific release lacks art)
    if release_group_id:
        try:
            data = mb.get_release_group_image_front(release_group_id, size='500')
            time.sleep(1)
            if data:
                return bytes(data), _mime(bytes(data))
        except mb.ResponseError as exc:
            print(f'    CAA release-group error ({str(exc)[:40]}), skipping cover art.',
                  file=sys.stderr)

    return b'', ''


def fetch_work_detail(work_id: str) -> dict:
    """Fetch a work with its artist relationships and parent work links.
    Results are cached in-process so shared parent works (e.g. a symphonic
    poem that is the parent of four movements) are only fetched once."""
    if work_id in _WORK_CACHE:
        return _WORK_CACHE[work_id]
    result = _mb_get(
        mb.get_work_by_id, work_id,
        includes=['artist-rels', 'work-rels', 'url-rels', 'tags', 'aliases'],
    )
    time.sleep(1)
    work = result.get('work', {})
    _WORK_CACHE[work_id] = work
    return work

# ---------------------------------------------------------------------------
# Artist / performer classification helpers  (CE-style)
# ---------------------------------------------------------------------------

def _is_ensemble(name: str) -> bool:
    low = name.lower()
    return any(s in low for s in ENSEMBLE_STRINGS)


def _is_choir(name: str) -> bool:
    low = name.lower()
    return any(s in low for s in CHOIR_STRINGS)


def _is_orchestra(name: str) -> bool:
    low = name.lower()
    return any(s in low for s in ORCHESTRA_STRINGS)


def artist_credit_phrase(credit_list) -> str:
    parts = []
    for item in credit_list:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            parts.append(item.get('name') or item['artist']['name'])
    return ''.join(parts)


def artist_ids(credit_list) -> list:
    return [
        item['artist']['id']
        for item in credit_list
        if isinstance(item, dict) and 'artist' in item
    ]


def artist_sort_names(credit_list) -> list:
    return [
        item['artist'].get('sort-name', item['artist']['name'])
        for item in credit_list
        if isinstance(item, dict) and 'artist' in item
    ]


def last_name(sort_name: str) -> str:
    """Extract last name from a sort-name ('Surname, Forename')."""
    return sort_name.split(',')[0].strip()

# ---------------------------------------------------------------------------
# Work hierarchy builder  (CE _cwp_ convention)
# ---------------------------------------------------------------------------

def build_work_hierarchy(work: dict, visited: set | None = None) -> list:
    """
    Walk up the MB work parent chain, returning a list from bottom (index 0)
    to top (highest index).  Each element is the full work dict.

    CE convention: _cwp_work_0 = bottom (recording's direct work),
                   _cwp_work_N = top (root work).
    """
    if visited is None:
        visited = set()
    wid = work.get('id', '')
    if wid in visited:
        return [work]
    visited.add(wid)

    hierarchy = [work]

    # Work-rels that point upward (type "parts" / "part of")
    for rel in work.get('work-relation-list', []):
        if rel.get('direction') == 'backward' and rel.get('type') in ('parts', 'part of'):
            parent_id = rel.get('work', {}).get('id', '')
            if parent_id and parent_id not in visited:
                print(f'      Fetching parent work {parent_id} …')
                parent = fetch_work_detail(parent_id)
                parent_hierarchy = build_work_hierarchy(parent, visited)
                hierarchy.extend(parent_hierarchy)
                break  # take first parent only (handle multiple parents as multi-value)

    return hierarchy


def strip_common_prefix(child: str, parent: str) -> str:
    """
    CE _cwp_part_n logic: remove from child any text that duplicates parent,
    producing a short movement/part label.
    """
    if not parent or not child:
        return child
    # Remove leading parent text from child (case-insensitive)
    low_c = child.lower()
    low_p = parent.lower()
    if low_c.startswith(low_p):
        stripped = child[len(parent):].lstrip(' :.-–—,')
        return stripped if stripped else child
    # Try stripping up to the first colon in child
    if ':' in child:
        after_colon = child.split(':', 1)[1].strip()
        return after_colon if after_colon else child
    return child


def period_for_year(year: int | None) -> str:
    if year is None:
        return ''
    for name, start, end in PERIOD_MAP:
        if start <= year <= end:
            return name
    return ''

# ---------------------------------------------------------------------------
# Main metadata builder
# ---------------------------------------------------------------------------

def extract_work_artist_rels(work: dict, role_buckets: dict):
    """
    Fill role_buckets (dict of role→list of (name, sort_name, mbid)) from
    a work's artist-relation-list.  Follows CE _cwp_ convention.

    Deduplicates by artist MBID within each role bucket to avoid the same
    composer appearing multiple times when they are credited at every level
    of the work hierarchy (e.g. Respighi appears on both the movement and
    the parent symphonic poem).
    """
    def _seen_ids(bucket):
        return {e[2] for e in bucket if e[2]}

    for rel in work.get('artist-relation-list', []):
        rtype = rel.get('type', '')
        artist = rel.get('artist', {})
        name = artist.get('name', '')
        sort = artist.get('sort-name', name)
        mid = artist.get('id', '')
        entry = (name, sort, mid)

        if rtype in ('composer', 'writer'):
            if mid not in _seen_ids(role_buckets['composers']):
                role_buckets['composers'].append(entry)
        elif rtype == 'lyricist':
            if mid not in _seen_ids(role_buckets['lyricists']):
                role_buckets['lyricists'].append(entry)
        elif rtype == 'librettist':
            if mid not in _seen_ids(role_buckets['librettists']):
                role_buckets['librettists'].append(entry)
        elif rtype == 'translator':
            if mid not in _seen_ids(role_buckets['translators']):
                role_buckets['translators'].append(entry)
        elif rtype in ('arranger', 'instrument arranger', 'vocal arranger'):
            if mid not in _seen_ids(role_buckets['arrangers']):
                role_buckets['arrangers'].append(entry)
        elif rtype == 'orchestrator':
            if mid not in _seen_ids(role_buckets['orchestrators']):
                role_buckets['orchestrators'].append(entry)
        elif rtype == 'reconstructed by':
            if mid not in _seen_ids(role_buckets['reconstructors']):
                role_buckets['reconstructors'].append(entry)
        elif rtype == 'revised by':
            if mid not in _seen_ids(role_buckets['revisors']):
                role_buckets['revisors'].append(entry)


def collect_work_dates(work: dict) -> dict:
    """Return dict with composed/published/premiered dates from work attributes."""
    dates = {}
    for attr in work.get('attribute-list', []):
        val = attr.get('value', '')
        t = attr.get('type', '').lower()
        if 'composed' in t or 'composition' in t:
            dates['composed'] = val
        elif 'published' in t or 'publish' in t:
            dates['published'] = val
        elif 'premiered' in t or 'premiere' in t:
            dates['premiered'] = val
    # Also check the work's begin/end life-span used for dates
    lifespan = work.get('life-span', {})
    if lifespan.get('begin') and 'composed' not in dates:
        dates['composed'] = lifespan['begin'][:4]
    return dates


def parse_year(date_str: str) -> int | None:
    if not date_str:
        return None
    m = re.match(r'(\d{4})', date_str)
    return int(m.group(1)) if m else None


def collect_work_tags_and_key(work: dict) -> tuple[list, str]:
    """Return (folksonomy_tags, key_signature)."""
    tags = [t.get('name', '') for t in work.get('tag-list', [])]
    key = work.get('key', '') or work.get('attribute-list', [{}])[0].get('value', '')
    # Try to find key in attribute-list
    for attr in work.get('attribute-list', []):
        if attr.get('type', '').lower() in ('key', 'key signature'):
            key = attr.get('value', '')
    return tags, key


def build_track_metadata(release: dict, track: dict, medium_pos: int,
                         recording_detail: dict, work_hierarchy: list) -> dict:
    """
    Build the complete flat tag dict for one track, implementing all
    Classical Extras _cwp_ and _cea_ conventions.
    """
    rec = track['recording']
    rg = release.get('release-group', {})

    # ---- Release-level artists ----
    release_artists = release.get('artist-credit', [])
    album_artist_phrase = artist_credit_phrase(release_artists)
    album_artist_ids_str = '/'.join(artist_ids(release_artists))
    album_artist_sort = '; '.join(artist_sort_names(release_artists))

    # ---- Recording artist credit ----
    rec_artists = rec.get('artist-credit', [])
    rec_artist_phrase = artist_credit_phrase(rec_artists)
    rec_artist_ids_str = '/'.join(artist_ids(rec_artists))
    rec_artist_sort = '; '.join(artist_sort_names(rec_artists))

    # ---- Label / catalogue ----
    label_info_list = release.get('label-info-list', [])
    label_info = label_info_list[0] if label_info_list else {}
    label_name = label_info.get('label', {}).get('name', '')
    catalog_number = label_info.get('catalog-number', '')

    # ---- Track counts ----
    medium = next(
        (m for m in release.get('medium-list', []) if int(m['position']) == medium_pos),
        {}
    )
    total_tracks = str(len(medium.get('track-list', [])))

    # ---- Recording artist-rels from detail lookup ----
    # CE _cea_ classification
    cea = defaultdict(list)   # cea_soloists, cea_conductors, etc.

    for rel in recording_detail.get('artist-relation-list', []):
        rtype = rel.get('type', '')
        artist = rel.get('artist', {})
        name = artist.get('name', '')
        sort = artist.get('sort-name', name)
        mid = artist.get('id', '')

        entry = {'name': name, 'sort': sort, 'id': mid}

        if rtype == 'conductor':
            cea['conductors'].append(entry)
        elif rtype == 'chorus master':
            cea['chorusmasters'].append(entry)
        elif rtype == 'concertmaster':
            cea['leaders'].append(entry)
        elif rtype in ('arranger', 'instrument arranger', 'vocal arranger'):
            cea['arrangers'].append(entry)
        elif rtype == 'orchestrator':
            cea['orchestrators'].append(entry)
        elif rtype in ('composer', 'writer'):
            cea['composers'].append(entry)
        elif rtype == 'producer':
            cea['producers'].append(entry)
        elif rtype == 'balance':
            cea['engineers'].append(entry)
        elif rtype in ('performer', 'instrument', 'vocal', 'performing orchestra'):
            if _is_ensemble(name):
                cea['ensembles'].append(entry)
            else:
                attrs = rel.get('attribute-list', [])
                first_attr = attrs[0] if attrs else ''
                instr = first_attr if isinstance(first_attr, str) else first_attr.get('value', '') if first_attr else ''
                entry['instrument'] = instr
                if any(v in instr.lower() for v in ('soprano', 'mezzo', 'tenor', 'baritone', 'bass', 'contralto',
                                                     'voice', 'vocal', 'singer')):
                    cea['vocalists'].append(entry)
                elif instr:
                    cea['instrumentalists'].append(entry)
                else:
                    cea['other_soloists'].append(entry)

    # All soloists combined (non-ensemble, non-conductor)
    all_soloists = cea['vocalists'] + cea['instrumentalists'] + cea['other_soloists']
    cea['soloists'] = all_soloists

    # ---- Work hierarchy (CE _cwp_ levels) ----
    cwp = {}
    role_buckets = defaultdict(list)  # composers, lyricists, …

    if work_hierarchy:
        n_levels = len(work_hierarchy)
        cwp['cwp_part_levels'] = str(n_levels - 1)

        for i, work in enumerate(work_hierarchy):
            wname = work.get('title', '')
            wid = work.get('id', '')
            cwp[f'cwp_work_{i}'] = wname
            cwp[f'cwp_workid_{i}'] = wid
            extract_work_artist_rels(work, role_buckets)

            # Dates and key for level 0 (bottom) work
            if i == 0:
                dates = collect_work_dates(work)
                if dates.get('composed'):
                    cwp['cwp_composed_dates'] = dates['composed']
                if dates.get('published'):
                    cwp['cwp_published_dates'] = dates['published']
                if dates.get('premiered'):
                    cwp['cwp_premiered_dates'] = dates['premiered']
                tags, key = collect_work_tags_and_key(work)
                if key:
                    cwp['cwp_keys'] = key
                # Work type → genre
                wtype = work.get('type', '')
                if wtype:
                    cwp['cwp_worktype_genres'] = wtype

        top_work = work_hierarchy[-1]
        cwp['cwp_work_top'] = top_work.get('title', '')
        cwp['cwp_workid_top'] = top_work.get('id', '')

        # _cwp_part_n: stripped movement names
        for i in range(n_levels):
            if i < n_levels - 1:
                parent_name = cwp.get(f'cwp_work_{i+1}', '')
            else:
                parent_name = ''
            cwp[f'cwp_part_{i}'] = strip_common_prefix(cwp.get(f'cwp_work_{i}', ''), parent_name)

        # CE: _cwp_work = top-level canonical work name
        # _cwp_groupheading = "Top :: [Intermediate ::] Movement"
        #   where "Movement" is the stripped part (cwp_part_0), not the raw
        #   bottom work name which typically repeats the parent name as a prefix.
        if n_levels == 1:
            cwp['cwp_work'] = cwp.get('cwp_work_0', '')
            cwp['cwp_groupheading'] = cwp.get('cwp_work_0', '')
            cwp['cwp_part'] = ''
        else:
            top_name = cwp.get(f'cwp_work_{n_levels - 1}', '')
            cwp['cwp_work'] = top_name

            # Build groupheading from top → intermediate stripped names → bottom stripped name
            gh_parts = [top_name]
            # Intermediate levels (indices n_levels-2 down to 1): use stripped part names
            for j in range(n_levels - 2, 0, -1):
                inter_part = cwp.get(f'cwp_part_{j}', cwp.get(f'cwp_work_{j}', ''))
                if inter_part:
                    gh_parts.append(inter_part)
            # Bottom level (index 0): always use stripped part
            bottom_part = cwp.get('cwp_part_0', '')
            if bottom_part:
                gh_parts.append(bottom_part)
            cwp['cwp_groupheading'] = ' :: '.join(gh_parts)
            cwp['cwp_part'] = bottom_part

        # Intermediate works (between movement and top): use stripped part names
        if n_levels > 2:
            inter = [cwp.get(f'cwp_part_{j}', cwp.get(f'cwp_work_{j}', ''))
                     for j in range(1, n_levels - 1)]
            cwp['cwp_inter_work'] = ' :: '.join(p for p in inter if p)

        # Period from composed date
        year = parse_year(cwp.get('cwp_composed_dates', ''))
        period = period_for_year(year)
        if period:
            cwp['period'] = period

    # Work-level artist roles (CE _cwp_composers etc.)
    if role_buckets['composers']:
        cwp['cwp_composers'] = '; '.join(e[0] for e in role_buckets['composers'])
        cwp['cwp_composers_sort'] = '; '.join(e[1] for e in role_buckets['composers'])
        cwp['cwp_composer_lastnames'] = '; '.join(last_name(e[1]) for e in role_buckets['composers'])
    for key in ('arrangers', 'orchestrators', 'reconstructors', 'revisors',
                 'lyricists', 'librettists', 'translators'):
        bucket = role_buckets[key]
        if bucket:
            cwp[f'cwp_{key}'] = '; '.join(e[0] for e in bucket)
            cwp[f'cwp_{key}_sort'] = '; '.join(e[1] for e in bucket)

    # ---- Work-relation lookup: direct performance work link ----
    direct_work_id = ''
    direct_work_title = ''
    for rel in recording_detail.get('work-relation-list', []):
        if rel.get('type') == 'performance':
            direct_work_id = rel.get('work', {}).get('id', '')
            direct_work_title = rel.get('work', {}).get('title', '')
            break

    # ---- Derive COMPOSER, CONDUCTOR for standard tags ----
    # Priority: work-level composers → recording-level composers → album artist (if person)
    composer_name = ''
    composer_sort = ''
    composer_id = ''
    if role_buckets['composers']:
        composer_name = '; '.join(e[0] for e in role_buckets['composers'])
        composer_sort = '; '.join(e[1] for e in role_buckets['composers'])
        composer_id = '/'.join(e[2] for e in role_buckets['composers'])
    elif cea['composers']:
        composer_name = '; '.join(e['name'] for e in cea['composers'])
        composer_sort = '; '.join(e['sort'] for e in cea['composers'])
        composer_id = '/'.join(e['id'] for e in cea['composers'])

    conductor_name = '; '.join(e['name'] for e in cea['conductors'])
    conductor_id = '/'.join(e['id'] for e in cea['conductors'])
    chorusmaster = '; '.join(e['name'] for e in cea['chorusmasters'])
    leader = '; '.join(e['name'] for e in cea['leaders'])

    # ---- Arranger / orchestrator (CE: annotated with role in parens) ----
    arranger_parts = []
    for e in cea['arrangers']:
        arranger_parts.append(e['name'])
    for e in role_buckets['arrangers']:
        if e[0] not in arranger_parts:
            arranger_parts.append(e[0])
    for e in role_buckets['orchestrators']:
        if e[0] not in arranger_parts:
            arranger_parts.append(f'{e[0]} (orch.)')
    for e in role_buckets['reconstructors']:
        arranger_parts.append(f'{e[0]} (reconstructed)')
    for e in role_buckets['revisors']:
        arranger_parts.append(f'{e[0]} (revised)')
    arranger_str = '; '.join(arranger_parts)

    lyricist_str = '; '.join(e[0] for e in role_buckets['lyricists'] + role_buckets['librettists'])
    translator_str = '; '.join(e[0] for e in role_buckets['translators'])

    # ---- Performer classification (CE _cea_ style) ----
    soloist_names = [e['name'] for e in all_soloists]
    soloist_str = '; '.join(
        f'{e["name"]} ({e["instrument"]})' if e.get("instrument") else e["name"]
        for e in all_soloists
    )
    ensemble_names = [e['name'] for e in cea['ensembles']]
    ensemble_str = '; '.join(ensemble_names)
    vocalist_str = '; '.join(
        f'{e["name"]} ({e["instrument"]})' if e.get("instrument") else e["name"]
        for e in cea['vocalists']
    )
    instrumentalist_str = '; '.join(
        f'{e["name"]} ({e["instrument"]})' if e.get("instrument") else e["name"]
        for e in cea['instrumentalists']
    )
    instruments_str = '; '.join(
        e.get('instrument', '') for e in all_soloists if e.get('instrument')
    )

    # Recording artist = all non-conductor, non-ensemble performing artists
    recording_artist_names = [e['name'] for e in all_soloists + cea['ensembles']]
    if cea['conductors']:
        recording_artist_names += [e['name'] for e in cea['conductors']]
    cea_recording_artist = '; '.join(recording_artist_names) or rec_artist_phrase

    # ---- ALBUM / WORK / MOVEMENT final derivation ----
    # work tag = top-level canonical work (CE cwp_work_top, or cwp_work_N)
    work_tag = cwp.get('cwp_work_top') or cwp.get('cwp_work_0') or direct_work_title or ''
    # groupheading = full hierarchy joined with ::
    groupheading = cwp.get('cwp_groupheading', work_tag)
    # movement/part = stripped bottom-level
    part_tag = cwp.get('cwp_part', cwp.get('cwp_part_0', ''))

    # Genre from worktype
    wtype_genre = WORKTYPE_GENRES.get(cwp.get('cwp_worktype_genres', ''), '')
    genre = wtype_genre or 'Classical'

    # ---- Assemble full tag dict ----
    # Store raw lists as private keys (underscore prefix = not written to file)
    meta = {
        '_cea_conductors_list': cea['conductors'],
        '_cea_ensembles_list': cea['ensembles'],
        # ── Standard Picard tags ──────────────────────────────────────────
        'TITLE': rec['title'],
        'ARTIST': rec_artist_phrase,
        'ARTISTS': rec_artist_phrase,
        'ARTISTSORT': rec_artist_sort,
        'ALBUMARTIST': album_artist_phrase,
        'ALBUMARTISTSORT': album_artist_sort,
        'ALBUM': release['title'],
        'TRACKNUMBER': str(track['position']),
        'TOTALTRACKS': total_tracks,
        'DISCNUMBER': str(medium_pos),
        'DATE': release.get('date', ''),
        'ORIGINALDATE': rg.get('first-release-date', ''),
        'MEDIA': 'CD',
        'SCRIPT': release.get('text-representation', {}).get('script', ''),
        'LANGUAGE': release.get('text-representation', {}).get('language', ''),
        'RELEASETYPE': rg.get('primary-type', ''),
        'RELEASESTATUS': release.get('status', ''),

        # Label / catalogue
        'ORGANIZATION': label_name,
        'LABEL': label_name,
        'CATALOGNUMBER': catalog_number,
        'BARCODE': release.get('barcode', ''),

        # Work / movement (CE cwp_work_tag_multi default: groupheading, work)
        'WORK': work_tag,
        'GROUPHEADING': groupheading,
        'TOP_WORK': cwp.get('cwp_work_top', work_tag),

        # CE movement tags: part (exc. num), movement/part/subtitle (inc. num)
        'PART': part_tag,
        'MOVEMENT': part_tag,
        'SUBTITLE': part_tag,

        # Composer / conductor / performers
        'COMPOSER': composer_name,
        'COMPOSERSORT': composer_sort,
        'CONDUCTOR': conductor_name,
        'LYRICIST': lyricist_str,
        'TRANSLATOR': translator_str,
        'ARRANGER': arranger_str,
        'CHORUSMASTER': chorusmaster,
        'LEADER': leader,

        # Performer lists (CE cea_ convention as writable tags)
        'SOLOISTS': soloist_str,
        'ENSEMBLE': ensemble_str,
        'BAND': ensemble_str,
        'VOCALISTS': vocalist_str,
        'INSTRUMENTALISTS': instrumentalist_str,
        'INSTRUMENT': instruments_str,

        # Genre / period / key
        'GENRE': genre,
        'PERIOD': cwp.get('period', ''),
        'KEY': cwp.get('cwp_keys', ''),
        'IS_CLASSICAL': '1',

        # Work dates
        'WORK_YEAR': (cwp.get('cwp_composed_dates')
                      or cwp.get('cwp_published_dates')
                      or cwp.get('cwp_premiered_dates', '')),
        'COMPOSED_DATE': cwp.get('cwp_composed_dates', ''),
        'PUBLISHED_DATE': cwp.get('cwp_published_dates', ''),
        'PREMIERED_DATE': cwp.get('cwp_premiered_dates', ''),

        # Production credits
        'PRODUCER': '; '.join(e['name'] for e in cea['producers']),
        'ENGINEER': '; '.join(e['name'] for e in cea['engineers']),

        # ── MusicBrainz ID tags (Picard standard) ────────────────────────
        'MUSICBRAINZ_ALBUMID': release['id'],
        'MUSICBRAINZ_TRACKID': track['id'],
        'MUSICBRAINZ_RECORDINGID': rec['id'],
        'MUSICBRAINZ_RELEASEGROUPID': rg.get('id', ''),
        'MUSICBRAINZ_ALBUMARTISTID': album_artist_ids_str,
        'MUSICBRAINZ_ARTISTID': rec_artist_ids_str,
        'MUSICBRAINZ_WORKID': direct_work_id or cwp.get('cwp_workid_0', ''),
        'MUSICBRAINZ_CONDUCTORID': conductor_id,
        'MUSICBRAINZ_COMPOSERID': composer_id,
        'MUSICBRAINZ_RELEASETRACKID': track['id'],

        # ── Classical Extras _cea_ tags (written without leading underscore) ──
        'CEA_RECORDING_ARTIST': cea_recording_artist,
        'CEA_SOLOISTS': soloist_str,
        'CEA_SOLOIST_NAMES': '; '.join(soloist_names),
        'CEA_VOCALISTS': vocalist_str,
        'CEA_INSTRUMENTALISTS': instrumentalist_str,
        'CEA_OTHER_SOLOISTS': '; '.join(e['name'] for e in cea['other_soloists']),
        'CEA_ENSEMBLES': ensemble_str,
        'CEA_ENSEMBLE_NAMES': '; '.join(ensemble_names),
        'CEA_CONDUCTORS': conductor_name,
        'CEA_COMPOSERS': cwp.get('cwp_composers', composer_name),
        'CEA_COMPOSER_LASTNAMES': cwp.get('cwp_composer_lastnames', last_name(composer_sort)),
        'CEA_PERFORMERS': rec_artist_phrase,
        'CEA_ARRANGERS': arranger_str,
        'CEA_ORCHESTRATORS': '; '.join(e[0] for e in role_buckets['orchestrators']),
        'CEA_CHORUSMASTERS': chorusmaster,
        'CEA_LEADERS': leader,
        'CEA_INSTRUMENTS': instruments_str,

        # ── Classical Extras _cwp_ work-hierarchy tags ────────────────────
        'CWP_WORK_TOP': cwp.get('cwp_work_top', ''),
        'CWP_WORKID_TOP': cwp.get('cwp_workid_top', ''),
        'CWP_PART_LEVELS': cwp.get('cwp_part_levels', '0'),
        'CWP_PART': cwp.get('cwp_part', ''),
        'CWP_WORK': cwp.get('cwp_work', ''),
        'CWP_GROUPHEADING': cwp.get('cwp_groupheading', ''),
        'CWP_INTER_WORK': cwp.get('cwp_inter_work', ''),
        'CWP_COMPOSERS': cwp.get('cwp_composers', ''),
        'CWP_COMPOSERS_SORT': cwp.get('cwp_composers_sort', ''),
        'CWP_COMPOSER_LASTNAMES': cwp.get('cwp_composer_lastnames', ''),
        'CWP_ARRANGERS': cwp.get('cwp_arrangers', ''),
        'CWP_ORCHESTRATORS': cwp.get('cwp_orchestrators', ''),
        'CWP_LYRICISTS': cwp.get('cwp_lyricists', ''),
        'CWP_LIBRETTISTS': cwp.get('cwp_librettists', ''),
        'CWP_TRANSLATORS': cwp.get('cwp_translators', ''),
        'CWP_KEYS': cwp.get('cwp_keys', ''),
        'CWP_COMPOSED_DATES': cwp.get('cwp_composed_dates', ''),
        'CWP_PUBLISHED_DATES': cwp.get('cwp_published_dates', ''),
        'CWP_PREMIERED_DATES': cwp.get('cwp_premiered_dates', ''),
        'CWP_WORKTYPE_GENRES': cwp.get('cwp_worktype_genres', ''),
    }

    # Add per-level cwp_work_N / cwp_workid_N / cwp_part_N
    for key, val in cwp.items():
        if re.match(r'cwp_(work|workid|part)_\d+$', key):
            meta[key.upper()] = val

    return meta


# ---------------------------------------------------------------------------
# Directory / filename helpers
# ---------------------------------------------------------------------------

SAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(s: str, max_len: int = 80) -> str:
    s = SAFE_RE.sub('_', s).strip('. ')
    return s[:max_len]


def build_dest_path(dest_root: Path, release: dict, track: dict, meta: dict) -> Path:
    """
    CE-style layout:
      <Composer sort-name> - <Performers> /
        <Work title> (<work MBID>) /
          <nn> - <movement title>.<ext>

    The numeric prefix is the movement number within the work (MOVEMENTNUMBER),
    not the album track number.  Width is 2 digits normally; 3 digits when the
    work contains more than 99 movements (MOVEMENTTOTAL > 99).
    """
    # Composer: from cwp_composers (last names) or albumartist — deduplicated
    raw_composer = meta.get('CWP_COMPOSER_LASTNAMES') or meta.get('CEA_COMPOSER_LASTNAMES', '')
    if raw_composer:
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for part in raw_composer.split('; '):
            part = part.strip()
            if part and part not in seen:
                seen.add(part)
                unique.append(part)
        composer = '; '.join(unique)
    else:
        # fall back: find Person-type artist in release credit
        composer = ''
        for item in release.get('artist-credit', []):
            if isinstance(item, dict):
                a = item.get('artist', {})
                if a.get('type') == 'Person':
                    composer = a.get('sort-name', a.get('name', ''))
                    break
        if not composer:
            composer = 'Unknown Composer'

    # Performers: prefer conductor + ensemble over raw recording artist phrase
    conductors = [e['name'] for e in meta.get('_cea_conductors_list', [])]
    ensembles = [e['name'] for e in meta.get('_cea_ensembles_list', [])]
    if conductors or ensembles:
        performers = '; '.join(conductors + ensembles)
    else:
        performers = meta.get('CEA_ENSEMBLE_NAMES') or meta.get('ARTIST', 'Unknown Performers')

    # Work: top canonical work title (with MBID suffix for uniqueness)
    work_title = meta.get('CWP_WORK_TOP') or meta.get('WORK', '')
    work_mbid = meta.get('CWP_WORKID_TOP') or meta.get('MUSICBRAINZ_WORKID', '')

    work_dir = safe_name(work_title)
    if work_mbid:
        work_dir = f'{work_dir} ({work_mbid})'

    top_dir = safe_name(f'{composer} - {performers}')

    # Use movement number within the work, not the album track position.
    # Width: 2 digits for works up to 99 movements, 3 digits beyond that.
    movt_num = int(meta.get('MOVEMENTNUMBER') or track['position'])
    movt_tot = int(meta.get('MOVEMENTTOTAL') or 1)
    width = 3 if movt_tot > 99 else 2
    track_num = str(movt_num).zfill(width)

    track_title = safe_name(meta.get('TITLE', rec_title(track)))

    return dest_root / top_dir / work_dir / f'{track_num} - {track_title}'


def rec_title(track: dict) -> str:
    return track.get('recording', {}).get('title', 'Unknown')


# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------

def apply_tags_flac(dest_file: Path, meta: dict,
                    cover_data: bytes = b'', cover_mime: str = ''):
    audio = FLAC(str(dest_file))
    audio.clear()
    for key, value in meta.items():
        if key.startswith('_'):
            continue  # private/internal keys, not written to file
        if value:
            audio[key.lower()] = str(value)
    if cover_data:
        pic = FLACPicture()
        pic.type = 3           # 3 = Cover (front), matching ID3 APIC type 3
        pic.mime = cover_mime or 'image/jpeg'
        pic.desc = 'Cover'
        pic.width = 0          # unknown; players don't require this to be exact
        pic.height = 0
        pic.depth = 0
        pic.colors = 0
        pic.data = cover_data
        audio.add_picture(pic)
    audio.save()


def apply_tags_mp3(dest_file: Path, meta: dict,
                   cover_data: bytes = b'', cover_mime: str = ''):
    try:
        audio = MP3(str(dest_file))
        if audio.tags:
            audio.tags.delete(str(dest_file))
    except Exception:
        pass

    tags = ID3()

    def txxx(desc, val):
        if val:
            tags.add(TXXX(encoding=3, desc=desc, text=val))

    # Standard frames
    if meta.get('TITLE'):      tags.add(TIT2(encoding=3, text=meta['TITLE']))
    if meta.get('ARTIST'):     tags.add(TPE1(encoding=3, text=meta['ARTIST']))
    if meta.get('ALBUMARTIST'): tags.add(TPE2(encoding=3, text=meta['ALBUMARTIST']))
    if meta.get('ALBUM'):      tags.add(TALB(encoding=3, text=meta['ALBUM']))
    if meta.get('TRACKNUMBER'):
        total = meta.get('TOTALTRACKS', '')
        tags.add(TRCK(encoding=3, text=f"{meta['TRACKNUMBER']}/{total}" if total else meta['TRACKNUMBER']))
    if meta.get('DISCNUMBER'): tags.add(TPOS(encoding=3, text=meta['DISCNUMBER']))
    if meta.get('DATE'):       tags.add(TDRC(encoding=3, text=meta['DATE']))
    if meta.get('ORIGINALDATE'): tags.add(TDOR(encoding=3, text=meta['ORIGINALDATE']))
    if meta.get('COMPOSER'):   tags.add(TCOM(encoding=3, text=meta['COMPOSER']))
    if meta.get('CONDUCTOR'):  tags.add(TPE3(encoding=3, text=meta['CONDUCTOR']))
    if meta.get('ORGANIZATION'): tags.add(TPUB(encoding=3, text=meta['ORGANIZATION']))

    # TXXX frames for everything else
    txxx_map = {
        'MUSICBRAINZ_ALBUMID':          'MusicBrainz Album Id',
        'MUSICBRAINZ_TRACKID':          'MusicBrainz Release Track Id',
        'MUSICBRAINZ_RECORDINGID':      'MusicBrainz Track Id',
        'MUSICBRAINZ_RELEASEGROUPID':   'MusicBrainz Release Group Id',
        'MUSICBRAINZ_ALBUMARTISTID':    'MusicBrainz Album Artist Id',
        'MUSICBRAINZ_ARTISTID':         'MusicBrainz Artist Id',
        'MUSICBRAINZ_WORKID':           'MusicBrainz Work Id',
        'MUSICBRAINZ_CONDUCTORID':      'MusicBrainz Conductor Id',
        'MUSICBRAINZ_COMPOSERID':       'MusicBrainz Composer Id',
        'CATALOGNUMBER':                'CATALOGNUMBER',
        'BARCODE':                      'BARCODE',
        'WORK':                         'WORK',
        'GROUPHEADING':                 'GROUPHEADING',
        'TOP_WORK':                     'TOP_WORK',
        'PART':                         'PART',
        'MOVEMENT':                     'MOVEMENT',
        'MOVEMENTNUMBER':               'MOVEMENTNUMBER',
        'MOVEMENTTOTAL':                'MOVEMENTTOTAL',
        'IS_CLASSICAL':                 'IS_CLASSICAL',
        'GENRE':                        'GENRE',
        'PERIOD':                       'PERIOD',
        'KEY':                          'KEY',
        'WORK_YEAR':                    'WORK_YEAR',
        'COMPOSED_DATE':                'COMPOSED_DATE',
        'LANGUAGE':                     'LANGUAGE',
        'SCRIPT':                       'SCRIPT',
        'RELEASETYPE':                  'MusicBrainz Album Type',
        'RELEASESTATUS':                'MusicBrainz Album Status',
        'SOLOISTS':                     'SOLOISTS',
        'ENSEMBLE':                     'ENSEMBLE',
        'CEA_RECORDING_ARTIST':         'CEA_RECORDING_ARTIST',
        'CEA_SOLOISTS':                 'CEA_SOLOISTS',
        'CEA_ENSEMBLES':                'CEA_ENSEMBLES',
        'CEA_CONDUCTORS':               'CEA_CONDUCTORS',
        'CEA_COMPOSERS':                'CEA_COMPOSERS',
        'CWP_WORK_TOP':                 'CWP_WORK_TOP',
        'CWP_GROUPHEADING':             'CWP_GROUPHEADING',
        'CWP_PART':                     'CWP_PART',
        'CWP_COMPOSERS':                'CWP_COMPOSERS',
        'CWP_KEYS':                     'CWP_KEYS',
        'CWP_COMPOSED_DATES':           'CWP_COMPOSED_DATES',
        'CWP_WORKTYPE_GENRES':          'CWP_WORKTYPE_GENRES',
    }
    for meta_key, txxx_desc in txxx_map.items():
        txxx(txxx_desc, meta.get(meta_key, ''))

    if cover_data:
        tags.add(APIC(
            encoding=3,                    # UTF-8
            mime=cover_mime or 'image/jpeg',
            type=3,                        # 3 = Cover (front)
            desc='Cover',
            data=cover_data,
        ))

    tags.save(str(dest_file), v2_version=4)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def find_source_files(src_dir: Path) -> list:
    exts = {'.flac', '.mp3', '.ogg', '.m4a', '.aac', '.wav'}
    return sorted(
        (p for p in src_dir.iterdir() if p.suffix.lower() in exts),
        key=lambda p: p.name,
    )


def run(release_id: str, src_dir: Path, dest_root: Path,
        user_agent: str, dry_run: bool = False, fetch_rels: bool = True):

    init_mb(user_agent)

    print(f'Fetching release {release_id} …')
    release = fetch_release(release_id)
    print(f'  "{release["title"]}"  ({release.get("date", "")})')

    # Flatten all tracks
    all_tracks = []
    for medium in release.get('medium-list', []):
        med_pos = int(medium['position'])
        for track in medium.get('track-list', []):
            track['_medium_pos'] = med_pos
            all_tracks.append(track)

    # ── Fetch cover art once for the whole release ───────────────────────
    rg_id = release.get('release-group', {}).get('id', '')
    cover_data, cover_mime = b'', ''
    if not dry_run:
        cover_data, cover_mime = fetch_cover_art(release_id, rg_id)
        if cover_data:
            print(f'  Cover art     : {len(cover_data):,} bytes ({cover_mime})')
        else:
            print('  Cover art     : not available', file=sys.stderr)

    src_files = find_source_files(src_dir)
    print(f'  Source files  : {len(src_files)}')
    print(f'  Release tracks: {len(all_tracks)}')

    if len(src_files) != len(all_tracks):
        print(
            f'WARNING: {len(src_files)} source files vs {len(all_tracks)} release tracks — '
            'matching by position up to the shorter count.', file=sys.stderr
        )

    pairs = list(zip(src_files, all_tracks))

    # ── Enrich each track with recording rels + work hierarchy ──────────
    if fetch_rels and not dry_run:
        print('\nFetching recording relationships and work hierarchies …')
        for src_file, track in pairs:
            rec = track['recording']
            rec_id = rec['id']
            print(f'  Track {track["position"]:2}: {rec["title"][:60]}')

            # 1. Recording detail (artist-rels, work-rels)
            rec_detail = fetch_recording_detail(rec_id)

            # 2. Walk work hierarchy
            work_hierarchy = []
            for rel in rec_detail.get('work-relation-list', []):
                if rel.get('type') == 'performance':
                    bottom_work_id = rel.get('work', {}).get('id', '')
                    if bottom_work_id:
                        print(f'      Fetching bottom work {bottom_work_id} …')
                        bottom_work = fetch_work_detail(bottom_work_id)
                        work_hierarchy = build_work_hierarchy(bottom_work)
                    break

            # 3. Build metadata
            meta = build_track_metadata(
                release, track, track['_medium_pos'],
                rec_detail, work_hierarchy,
            )

            # 4. Movement number/total: counted within top work across release
            # (placeholder — computed after all tracks processed)
            track['_meta'] = meta
            track['_work_hier'] = work_hierarchy

        # Compute movementnumber / movementtotal (CE cwp_movt_num / cwp_movt_tot)
        # Group tracks by top work MBID
        top_work_groups = defaultdict(list)
        for _, track in pairs:
            twid = track['_meta'].get('CWP_WORKID_TOP', '') or track['_meta'].get('MUSICBRAINZ_WORKID', '')
            top_work_groups[twid].append(track)

        for twid, group_tracks in top_work_groups.items():
            total = len(group_tracks)
            for idx, t in enumerate(group_tracks, start=1):
                t['_meta']['MOVEMENTNUMBER'] = str(idx)
                t['_meta']['MOVEMENTTOTAL'] = str(total)
                t['_meta']['CWP_MOVT_NUM'] = str(idx)
                t['_meta']['CWP_MOVT_TOT'] = str(total)
                # CE cwp_single_work_album
                t['_meta']['CWP_SINGLE_WORK_ALBUM'] = '1' if len(top_work_groups) == 1 else '0'

    else:
        # dry-run or no-fetch: basic metadata only
        for src_file, track in pairs:
            rec = track['recording']
            rec_artists = rec.get('artist-credit', [])
            rg = release.get('release-group', {})
            label_info = (release.get('label-info-list') or [{}])[0]
            track['_meta'] = {
                'TITLE': rec['title'],
                'ARTIST': artist_credit_phrase(rec_artists),
                'ALBUMARTIST': artist_credit_phrase(release.get('artist-credit', [])),
                'ALBUM': release['title'],
                'TRACKNUMBER': str(track['position']),
                'DATE': release.get('date', ''),
                'MUSICBRAINZ_ALBUMID': release['id'],
                'MUSICBRAINZ_RECORDINGID': rec['id'],
                'MUSICBRAINZ_TRACKID': track['id'],
                'RELEASETYPE': rg.get('primary-type', ''),
                'LABEL': label_info.get('label', {}).get('name', ''),
                'CATALOGNUMBER': label_info.get('catalog-number', ''),
                'BARCODE': release.get('barcode', ''),
                'IS_CLASSICAL': '1',
            }
            track['_work_hier'] = []

    # ── Copy and tag ─────────────────────────────────────────────────────
    print()
    for src_file, track in pairs:
        meta = track['_meta']
        dest_base = build_dest_path(dest_root, release, track, meta)
        dest_file = dest_base.with_suffix(src_file.suffix.lower())

        rel_path = dest_file.relative_to(dest_root) if not dry_run else dest_base
        print(f'  {src_file.name}')
        print(f'    → {rel_path}')

        if dry_run:
            print(f'    [dry-run] COMPOSER={meta.get("COMPOSER","")!r}  '
                  f'CONDUCTOR={meta.get("CONDUCTOR","")!r}  '
                  f'WORK={meta.get("WORK","")!r}  '
                  f'PERIOD={meta.get("PERIOD","")!r}')
            continue

        dest_file.parent.mkdir(parents=True, exist_ok=True)

        # Capture source timestamps before copying so we can restore them after
        # tagging (mutagen's .save() updates mtime on the destination).
        # Note: on Linux, ctime (inode-change time) is set by the kernel and
        # cannot be set by userspace; we preserve atime and mtime instead.
        src_stat = src_file.stat()
        src_times = (src_stat.st_atime, src_stat.st_mtime)

        shutil.copy2(src_file, dest_file)

        ext = src_file.suffix.lower()
        try:
            if ext == '.flac':
                apply_tags_flac(dest_file, meta, cover_data, cover_mime)
            elif ext == '.mp3':
                apply_tags_mp3(dest_file, meta, cover_data, cover_mime)
            else:
                print(f'    [skip tagging — unsupported format {ext}]')
        except Exception as exc:
            print(f'    ERROR tagging {dest_file.name}: {exc}', file=sys.stderr)

        # Restore source atime/mtime (tagging and cover art embedding bump mtime)
        os.utime(dest_file, src_times)

    print('\nDone.')
    if not dry_run:
        print(f'Output: {dest_root}')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Copy and tag a music album with MusicBrainz metadata (Classical Extras conventions).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--release-id', required=True,
                        help='MusicBrainz release MBID')
    parser.add_argument('--src-dir', required=True, type=Path,
                        help='Source directory containing audio files')
    parser.add_argument('--dest-dir', required=True, type=Path,
                        help='Root of destination music library')
    parser.add_argument('--user-agent',
                        default='MusicLibraryAnnotator/2.0 annotate_mb@example.com',
                        help='User-agent for MusicBrainz API')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview without copying or writing files')
    parser.add_argument('--no-fetch-rels', action='store_true',
                        help='Skip per-recording lookups (faster, less complete)')
    args = parser.parse_args()

    run(
        release_id=args.release_id,
        src_dir=args.src_dir,
        dest_root=args.dest_dir,
        user_agent=args.user_agent,
        dry_run=args.dry_run,
        fetch_rels=not args.no_fetch_rels,
    )


if __name__ == '__main__':
    main()
