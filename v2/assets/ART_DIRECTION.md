# River Race art direction

Generated with the built-in image generation tool. These are website-bound
3D-rendered assets, optimized to WebP while preserving the boat alpha channel.
The original PNG renders remain in the Codex generated-images directory.

## Final assets

- `river-world.webp`: 1536×1024 desktop world, 519 KB.
- `river-world-mobile.webp`: 1024×1536 portrait world, 497 KB.
- `royal-boat.webp`: 512×614 transparent blue/gold own-clan boat, 61 KB.
- `rival-boat.webp`: 512×614 transparent red/gold rival boat, 60 KB.
- `colosseum-world.webp`: 1536×1024 desktop water-arena, 375 KB.
- `colosseum-world-mobile.webp`: 1024×1536 portrait water-arena, 348 KB.

The two Colosseum worlds were generated with the built-in image tool using
the existing river world as the style reference and the supplied in-game
training/Colosseum screenshot as a concept reference. They are original
rendered environments, not cropped game screenshots. Converted to WebP at
quality 85; original PNGs are preserved outside the checkout.

## Final generation prompts

### Colosseum desktop world

Use case: stylized-concept. Asset type: production desktop game-world background for an interactive Clash Royale Colosseum leaderboard, NOT a UI mockup. Input image 1 is the STYLE REFERENCE (existing river world); image 2 is a CONCEPT REFERENCE only (actual game Colosseum). Primary request: bring the viewer INSIDE a majestic royal Colosseum with a water-filled central arena. Match exactly the vivid tactile Supercell Clash Royale 3D game rendering of reference 1: sculpted chunky bevelled grey stone, warm gold trims, saturated royal red and blue banners, soft upper-left sunlight, dimensional ambient occlusion, hand-painted material richness. Landscape 1536x1024, orthographic 65-degree overhead camera. The central turquoise water fills clear central 75% of width from y25% through bottom; all central water MUST be unobstructed for five separately animated HTML boat sprites and labels. Build beautiful encircling Colosseum arches, layered stadium terraces and fortified royal towers along only LEFT, RIGHT, and TOP edges, with a magnificent golden crown over the top central entry arch, statues/trophy details on side terraces, red/blue pennants, sandy carved stone promenades. Interior view, clearly a Colosseum not a river village. Symmetric powerful stadium architecture with rich depth and polished game-asset quality. No island or structure in center. No boats, people, text, interface, numbers, scoreboard, logos or watermark. Large uninterrupted open turquoise water lower-center. This is an actual separate environment bitmap to use beneath real live dashboard UI.

### Colosseum mobile world

Use case: stylized-concept. Asset type: production mobile portrait background for interactive Clash Royale Colosseum leaderboard. Supplied image is STYLE AND WORLD REFERENCE, not an edit target. Create a PORTRAIT 1024x1536 version of this same beautiful royal water-filled Colosseum INTERIOR. Same sculpted 3D stone, gold trim, blue/red banners, crown gateway, subtle firebraziers, polished Supercell Clash Royale materials and upper-left sunny lighting. Mobile layout is crucial: extremely narrow terraces/towers left and right at OUTER 7% of each side, uninterrupted central turquoise water occupies 86% of width from y22% to bottom. Crown gateway and curved tiers in top 20% only. Show a clearly enclosed Colosseum inside, royal stadium architecture NOT village houses or trees. Camera orthographic 65-degree overhead, boats will be overlaid separately in HTML and central water must be free of everything. Keep distinctive warm stone and opulent gold; vivid turquoise caustics and soft game-style rich shadows. Rendered professional game environment, no boats, no text, no people, no UI, no logos or watermark. Match reference exact art quality, architecture and color palette, but reorganize into tall portrait composition for five boats side-by-side.

### Desktop world

Use case: stylized-concept. Asset type: production background for an interactive Clash Royale River Race web dashboard, not a mockup. Create a premium Supercell / Clash Royale style 3D-rendered river race environment. Landscape 1536x1024 composition, orthographic overhead view at 65 degrees, top of world pointing up. Brilliant deep turquoise river occupies the entire CENTER 72% of image across width, clear uninterrupted water from bottom to top so five live boats can be overlaid in HTML. On the LEFT and RIGHT narrow banks: lush rounded lime-green stylized trees in clusters, bevelled grey rocks, golden sandy curved shoreline, little red-roofed medieval houses, carved wooden docks, a tiny blue-roofed castle on left and a decorated timber river port on right. At the very TOP CENTER: a richly modelled royal finish gate spanning the river, two chunky grey stone towers with blue/red conical roofs, gold trim, royal crown detail and glowing flags. Most of the middle must be WATER with subtle ripples and caustics, no objects in the central river. The professional art direction is exactly the tactile toy-like Supercell Clash Royale game aesthetic: rounded sculpted edges, dimensional stone blocks, saturated jewel colors, chunky geometry, hand painted material variation, strong ambient occlusion, soft directional sunlight from upper-left, rich depth, polished game art, detailed but clean composition. Everything is genuinely dimensional, no flat vector graphics, no low-poly triangulated style, no diagram. NO BOATS, no text, no UI panels, no numbers, no logos, no watermarks. This is the separate static landscape for an animated river race with foreground boat renders.

### Blue boat

Use case: stylized-concept. Asset type: single transparent game sprite for an interactive Clash Royale river race web dashboard. Generate ONE extraordinarily polished Supercell Clash Royale style royal war boat, isolated on genuine transparent background. The boat bow points toward the TOP of the image, stern toward BOTTOM. Camera is orthographic looking down at 65 degrees, same overhead 3/4 view as Clash Royale River Race. Full boat visible centered with 8% transparent margins. Tactile stylized 3D render with rounded bevels, strong ambient occlusion, rich clean materials, soft directional sunlight from upper-left. Thick warm carved oak hull with stacked golden wooden planks, metallic gold rim, curved pointed bow, sturdy rear deck. A rich royal BLUE cloth sail/canopy stretched between masts, gold crown crest on sail, little royal blue flag above. Large medieval shield with BLUE center and gold bevel mounted at front. Barrel and rope on deck, charming compact functional war vessel with large iconic readable silhouette. Smooth sculpted Supercell shapes, vivid blue and gold, professional finished game asset. No water, no background, no ground shadow, no text, no UI, no characters, no white outline, no checkerboard baked into image. Match the finish and material quality of the actual Clash Royale game boat, not a flat vector, not papercraft, not an amateur low-poly model. Boat points straight upward in frame.

### Red boat (blue boat used as edit target)

Use case: precise-object-edit. Asset type: transparent opposing-clan royal boat sprite. Change ONLY every royal blue cloth, blue flag and blue inset shield/straps to vivid Clash Royale rival RED. Keep absolutely everything else unchanged: same exact boat silhouette and camera, dimensions, wooden hull, GOLD crown emblems and gold rim metallic details, ropes, barrel, sunlight, materials, shadows and shading. Keep real transparent background and no water or cast ground shadow. This is a color variant of precisely the same game asset to display competing clans in a single animated river race.

### Mobile world (desktop world used as style reference)

Use case: compositing / stylized-concept. The supplied image is the STYLE AND WORLD REFERENCE. Create a PORTRAIT 1024x1536 mobile version of exactly this polished Supercell Clash Royale royal river world. Match the same tactile 3D game rendering, light, colors, chunky sculpted geometry, materials, trees, grey cliff rocks, wood docks, red roof houses, gold blue/red royal finish gate at the very top. Crucial mobile layout: an uninterrupted turquoise river covers central 86% of width from y20% down to bottom. Very NARROW visible green banks on left and right, about 7% width each, detailed but not intruding into center. Finish gate top center compressed to top 18% only, with castle towers near edges. The scene is much taller than wide, and composition is designed for five animated boats to be overlaid side-by-side. Keep all of central water clear of structures. A handful of tiny trees/rocks along edges retain the reference atmosphere. Beautiful dimensional caustics and small gentle ripples. No boats, no text, no UI, no labels, no logos. This must be a professional finished game environment, not a UI screenshot or a flat illustration. Preserve rich true dimensional rendering and reference art direction.
