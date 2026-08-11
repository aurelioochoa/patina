# Image prompts

Every image in the README was generated with Higgsfield (`nano_banana_pro`) and
then resized to 1800px wide and saved as JPEG. The banner is 21:9; the three
workflow diagrams are 16:9.

They share one visual language, and it is worth keeping: an aged copper plate
under soft raking light from the left, verdigris blooming at the edges, deep
near-black surround, photographic and tactile rather than rendered. The negatives
matter as much as the description — "no glow, no neon, no lens flare, no digital
or sci-fi effects" is what holds off the default AI-product look.

## Banner

`assets/banner.jpg` was generated in two steps.

The idea: patina is named for the finish that builds on a surface through use,
so the wordmark is not printed on the plate — it is the place where handling has
worn the tarnish back to bright copper. The thing the loop keeps is the thing
that gets touched. Clawd, the Claude Code mascot, is struck into the lower-left
corner as a maker's mark: whose workshop this came out of.

### Step 1 — the plate

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

### Step 2 — the maker's mark

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

### Notes for regenerating

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

## The diagrams

Three 16:9 plates, one per idea, each an engraved schematic rather than a mood
piece. The first attempt at these was a set of copper still lifes — a tray of
tags, a drawer of plates — and they were pretty and explained nothing. A README
image should carry the argument the paragraph next to it is making.

The move that makes it work: keep the banner's material language exactly (aged
copper, verdigris, raking light from the left, deep near-black surround) and
engrave a real flowchart into it. Boxes are rectangular outlines cut into the
metal; arrows are engraved lines with triangular heads; every cut is bright
unoxidised copper against the cool green, the same treatment as the wordmark.

### The text problem, met head-on

The banner notes above are about avoiding words, because a word placed near
another word loses letters. A diagram cannot avoid words. What holds them
together instead:

- **Spell every label out letter by letter** in the prompt — `Q-U-E-U-E` — and
  state that no letter may be added, invented, duplicated, merged, cropped or
  omitted.
- **Declare the closed set**: "the only text anywhere in the image is these five
  words". Without it the model garnishes the plate with plausible captions.
- **One word per box, centred and fully contained.** Labels that float free of a
  container drift and collide.
- **Keep the count low.** Five or six labels came back clean on the first try.
  This is the real budget — every additional word is another chance to lose one.
- **Say "square-on, flat, no perspective, no tilt".** The first queue plate came
  back photographed at a dramatic angle: handsome, and out of step with the
  others. Framing has to be specified or it wanders between images in a set.

### `loop.jpg` — the whole loop

Sits at the top of *How it works*. Six boxes in a ring, arrows clockwise, closed.

> A straight-on macro photograph of an aged copper plate lying flat and filling
> the frame, lit by soft raking light from the left. A technical diagram is
> engraved into the metal: six rectangular boxes arranged in a large ring,
> connected by engraved arrows all flowing clockwise to form one closed loop. The
> boxes are simple rectangular outlines cut into the copper; the arrows are
> engraved lines with clean triangular arrowheads. Every engraved line and letter
> is bright unoxidised copper where the tarnish has been cut away, warm and
> polished against the cool blue-green verdigris of the untouched plate.
>
> The six boxes are labelled, clockwise from the top: SESSION, REFLECT, PLACE,
> QUEUE, LIBRARY, CURATOR.
>
> CRITICAL: those six words are the only text anywhere in the image, spelled
> exactly S-E-S-S-I-O-N, R-E-F-L-E-C-T, P-L-A-C-E, Q-U-E-U-E, L-I-B-R-A-R-Y,
> C-U-R-A-T-O-R. Set them in a clean geometric monospace typeface, uppercase, one
> word per box, each word centred inside its own box and fully contained by it.
> Do not add, invent, duplicate, merge, crop or omit any letter or word. No other
> words, letters, numbers, captions or symbols anywhere on the plate.
>
> Verdigris blooms at the plate edges and thins toward the centre where the
> diagram sits. Deep near-black surround. Quiet, patient, museum-object mood — an
> engraver's schematic struck into metal, not a screen. Photographic and tactile:
> fine metal grain, micro-pitting, subtle surface scratches. No glow, no neon, no
> lens flare, no digital or sci-fi effects, no logos.

### `two-passes.jpg` — the boundary

The best of the three, and the one that got the most out of the material: asked
for a heavy engraved bar, the model cut the slot clean *through* the plate, so
the boundary is a hole in the metal and the LESSONS tag is the bridge across it.
Same prompt skeleton as above, with this body:

> On the LEFT half: two rectangular boxes joined by an engraved arrow, the first
> labelled SESSION, the second labelled REFLECT. On the RIGHT half: two
> rectangular boxes joined by an engraved arrow, the first labelled PLACE, the
> second labelled QUEUE.
>
> Between the two halves stands one heavy solid vertical bar cut deep into the
> copper, running the full height of the plate and dividing it completely in two.
> A single narrow gap is cut through the middle of that bar, and one engraved
> arrow passes through the gap from left to right, carrying a small rectangular
> tag labelled LESSONS. That arrow is the only thing that crosses the bar — no
> other line touches or passes it.
>
> CRITICAL: the only text anywhere in the image is the five words SESSION,
> REFLECT, LESSONS, PLACE, QUEUE, spelled exactly S-E-S-S-I-O-N, R-E-F-L-E-C-T,
> L-E-S-S-O-N-S, P-L-A-C-E, Q-U-E-U-E. […same closing as above…]

### `queue.jpg` — the fork

Two paths out of the queue that never rejoin. Regenerated once for framing; the
body that worked:

> A macro photograph of an aged copper plate photographed SQUARE-ON from directly
> above, perfectly flat and parallel to the camera, filling the frame as an exact
> rectangle with no tilt, no perspective, no angle, no foreshortening. […]
>
> On the left, one rectangular box labelled QUEUE. From it two engraved arrows
> diverge. The UPPER arrow rises to a box labelled APPROVE, and a further arrow
> leads right from that box to a final box labelled LIBRARY. The LOWER arrow
> descends to a box labelled REJECT, and a further arrow leads right from that
> box to a final box labelled DISCARD. Five boxes in total, arranged as one clean
> fork: a single source and two separate paths that never rejoin. The whole
> diagram is centred on the plate with generous bare margins.
>
> CRITICAL: the only text anywhere in the image is the five words QUEUE, APPROVE,
> LIBRARY, REJECT, DISCARD, spelled exactly Q-U-E-U-E, A-P-P-R-O-V-E,
> L-I-B-R-A-R-Y, R-E-J-E-C-T, D-I-S-C-A-R-D. […same closing as above…]

Check every label before shipping. A missing letter looks entirely plausible at a
glance, and these four came back clean only because they were read one word at a
time.
