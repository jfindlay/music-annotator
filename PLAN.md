# music-annotator — Development Plan

# Deferred / longer-term items

## Playlist generation for collections/cycles
The Ring cycle, Beethoven symphony cycles, etc. should be realised as **playlists** rather than
filesystem directories.  A collection-level directory layer was explicitly rejected.  Playlist
format (M3U, XSPF, or other) and generation logic TBD.

## Re-annotation / update mode
A mode to diff existing library tracks against updated MB / CAA / Discogs data and apply changes
with user confirmation.  Should check that embedded cover art is at original resolution and
replace thumbnails if found.

## Whipper integration
A mode that calls whipper (CD ripper) and passes the output directly into the annotation pipeline.
Journal events added for rip operations.

## Discogs integration
Fallback search and release creation support when MB has no entry.  Journal events added.

## AcoustID retry/backoff
The raw `urlopen()` in `fetch_acoustid_id` has no retry logic.  Should use `_mb_retry` or an
equivalent.

## `mb.get*() ; time.sleep()` consolidation
Factor the repeated `call ; sleep(1)` pattern into a single helper.

## Verify all CE-derived tags are always written
Audit that no MB-derived or CE-derived tag fields are silently dropped when their source data
is absent.  Every field should either be populated or explicitly set to `""`.
