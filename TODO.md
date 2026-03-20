- Add an execution mode for calling whipper and then running the tracks through the remainder of the `search` pipeline adding
  the tags, cover art, etc.  Update the library journal to include these events.
- Add Discogs integration for search help.  If a MusicBrainz entry cannot be found for the release, the user at this point could
  start creating one, using Discogs data as a reference.  Update the library journal to include these events.
- Add execution mode to diff the directory/file names+tags+cover art+etc. with latest MusicBrainz, Cover Art Archive, Discogs,
  etc. data.  Should the tracks keep an internal journal of metadata, like a git repo?  Probably not?
  - Make sure to check cover art image size in tracks is set to original size and replace it if not.
- Should retry/backoff for fingerprint raw `urlopen()`?
- User prompts should be more consistent in layout and coloring
- Factor `mb.get*() ; time.sleep()` into single statement?
- Don't encode release structures in the filesystem layout: they can be considered for playlist construction though
- Do these in a later implementation that consults wikipedia+imslp:
  - Composer, Conductor, soloist, ensemble, etc. names in their native language and native alphabet preferred if known
  - Work titles should be as the composer wrote them, for example Capriccio espagnole by Ри́мский-Ко́рсаков should be Каприччио на испанские темы, the composer's own title.  If a composer names a work with a different alphabet/language different from their native, for example, "II. Valse. Moderato. Tempo di Valse" from the Чайковского "Серенада для струнного оркестра", those are the title strings that should be used for the movement and work, respectively.
- replace musicbrainzngs dependency with musicbrainzngs2
