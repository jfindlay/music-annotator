# music-annotator — design notes and learnings

## Path is a handle, not a manifest

The destination directory and filename are *handles* — short, stable identifiers a user
locates a recording by — not *manifests* of every credited contributor.  Full credits
belong in tags.  The implication is structural and applies wherever path construction
touches metadata:

- **Performer component of the directory.** Source is ``release.artist_credit`` (the
  curator's resolution of primary attribution), not the union of every per-track credit.
  Named subgroups, additional choirs, and session soloists belong in tags, not the path.
  Concerto-like works are the exception: when the soloist is part of the work's canonical
  identity, it enters the path regardless of release-level crediting.
- **UX ceilings are real.** Music-player display widths and filesystem path limits cap the
  useful length of a dir/file name.  Any policy that admits unbounded credits into the path
  will eventually exceed both.  Mahler 8 with full credits is the worst case; the policy
  must keep that case readable.
- **Every dimension added to path construction needs a unification story.** MB credits
  movements of one recording inconsistently.  When a path-construction dimension can vary
  across movements of one work-group (composer with arranger/finisher attribute,
  recording-date interval, conductor/ensemble set), the existing pattern is a work-level
  unification pass in ``_pipeline.py`` — see the composer pass at lines 910-945 and the
  ``recording_date_work`` pass at lines 947-980.  Release-scoped sources (album-artist
  filter, release date) don't need a unification pass because they're already uniform.

## Classical Extras as editorial anchor

Every editorial decision on attribution, annotation, and path construction must refract
through the Classical Extras conventions
(github.com/metabrainz/picard-plugins/tree/2.0/plugins/classical_extras).  CE is not just
a tag-format spec — it encodes a coherent stance on how art-music recordings should be
described.  When a new rule (path-vs-tag split, soloist promotion, subgroup demotion) is
proposed, the validation step is: does CE's framing of the same question agree, and if
not, what's the principled reason for diverging?  The default is to follow CE; divergences
need a documented rationale.
