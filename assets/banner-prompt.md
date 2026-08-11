# Banner prompt

`assets/banner.jpg` was generated with Higgsfield (`nano_banana_pro`, 21:9, 2k),
in two steps, then resized to 1800px wide and saved as JPEG for the README.

The idea: patina is named for the finish that builds on a surface through use,
so the wordmark is not printed on the plate — it is the place where handling has
worn the tarnish back to bright copper. The thing the loop keeps is the thing
that gets touched. Clawd, the Claude Code mascot, is struck into the lower-left
corner as a maker's mark: whose workshop this came out of.

## Step 1 — the plate

> A wide, straight-on macro photograph of an aged copper plate, lit by soft
> raking light from the left. Blue-green verdigris patina blooms unevenly across
> the metal in organic, cloud-like drifts — thickest toward the edges, thinning
> toward the centre. The single lowercase word "patina" is set in a clean
> geometric monospace typeface across the middle of the plate. It is not printed
> or painted on: the letterforms are revealed, bright unoxidised copper where the
> tarnish has been worn away by years of handling, warm and faintly polished
> against the cool green. Very faint horizontal etched lines run behind the
> wordmark like engraved terminal text, extremely low contrast, almost
> subliminal. Deep near-black surround at the plate edges. Quiet, patient,
> museum-object mood. Photographic and tactile: fine metal grain, micro-pitting,
> subtle surface scratches. No glow, no neon, no lens flare, no digital or
> sci-fi effects, no extra words or logos.

## Step 2 — the maker's mark

Image-to-image with two references: step 1's job id, and a picture of Clawd
imported with `media_import_url`.

> Reproduce the FIRST reference photograph exactly: the weathered copper plate,
> same framing, same lighting, same verdigris pattern, and the word struck into
> it in bright unoxidised copper.
>
> CRITICAL: the word must remain exactly and completely "patina" — six letters,
> p a t i n a, in the same position, size, spacing and typeface as the reference.
> Do not replace, merge, overlap, crop or omit any letter. The word is untouched.
>
> The ONE addition: a SMALL maker's mark struck into the empty copper in the
> lower-left area of the plate, well below and well left of the word, in the
> blank space near the plate's bottom-left corner. It must not touch or crowd the
> word — leave a wide margin of bare plate between them. Make it small: about one
> third the height of the letters, the size of a stamped maker's mark on the back
> of a workshop tool.
>
> The mark is the blocky 8-bit pixel crab from the SECOND reference: a wide
> rectangular body with two tall narrow rectangular eye slots, one stubby
> rectangular claw jutting from each side, four short rectangular legs beneath.
> Keep its hard pixel geometry exactly — axis-aligned edges, square corners, no
> curves, no smoothing, no interior shading. It is struck into the metal like the
> letters: raised bright unoxidised copper, same bevelled edge, same soft shadow,
> same warm polished tone, same age and wear, faint verdigris creeping at its
> edges. Part of the original plate, not a sticker or overlay, not a real crab.
>
> Nothing else changes. No extra words, no starburst, no other symbols.

## Notes for regenerating

- **The mark and the wordmark compete, and the wordmark loses.** Every attempt to
  place a mark immediately beside the word ate a letter: three separate variants
  came back reading "atina", "panaa" and "ptina", each with the mark standing
  exactly where the missing letter had been. The edit is a regeneration, not a
  true inpaint, so anything placed on the word's baseline is negotiating for the
  same space.
- **Moving it to the bottom-left corner is what fixed it** — and it reads better
  anyway, as a stamped maker's mark rather than a logo lockup. Separating the two
  elements in space separates them in the model's attention.
- Spell the word out letter by letter in the prompt and say explicitly that no
  letter may be replaced, merged or omitted. Then check the output. Always check
  the output: a missing letter looks completely plausible at a glance.
- The etched horizontal lines are what keep step 1 from reading as generic
  weathered-metal stock. Keep them subliminal; asking for anything more legible
  produces fake code and misspelled words.
- Naming the negatives explicitly ("no glow, no neon, no lens flare") is what
  holds off the default AI-product look.
- Describing Clawd by his geometry as well as passing the reference is what keeps
  the pixel edges hard. Ask for a crab without that and you get a photographic
  one.
