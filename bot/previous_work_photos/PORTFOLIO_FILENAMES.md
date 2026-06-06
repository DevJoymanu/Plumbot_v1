# Portfolio image filenames

The bot can send individual portfolio pieces with their own title, price and
back-story. Metadata lives in `bot/portfolio_catalog.py`; the images must live
in this folder under the **exact filenames** below.

A catalogue item only becomes "sendable" once its file is present here
(`portfolio_catalog.available_items()` checks the disk), so the bot never
promises a photo it can't actually send.

Each catalogue item in `portfolio_catalog.py` points at one of the real image
files already in this folder (verified by eye). The mapping below is the source
of truth — if you rename or replace a photo, update the matching `filename` in
`portfolio_catalog.py` too.

| Catalogue item id            | Image file on disk                  | What the photo shows                                              |
|------------------------------|-------------------------------------|------------------------------------------------------------------|
| `modern-kitchen-island`      | `Full_kitchen_renovation.jpeg`      | White quartz island, pendant lights, built-in oven, gas hob      |
| `navy-shaker-kitchen`        | `Kitchen_installation.jpeg`         | Navy shaker kitchen, gas hob on island, patterned tile floor     |
| `freestanding-tub-hex`       | `standalone_freestanding_tub(2).jpg`| Oval white freestanding tub, black wall mixer, hex mosaic border  |
| `gold-tap-double-vanity`     | `custom_double_vanity.jpg`          | Gold square taps, twin white vessel basins, black granite top    |
| `black-granite-vanity-tub`   | `standalone_freestanding_tub.jpg`   | Black floating granite vanity + sculpted black freestanding tub  |
| `classic-toilet-basin`       | `chamber_and_sink.jpg`              | Close-coupled toilet + pedestal basin, beige tiles               |
| `clawfoot-tub-feature-wall`  | `full_bathroom_renovation.jpg`      | White clawfoot/roll-top tub, brick-effect wall, wall-hung WC     |
| `walk-in-rain-shower`        | `Cubicle.jpg`                       | Frameless glass walk-in rain shower, mosaic feature stripe       |
| `marble-builtin-tub`         | `ordinar_tub(built-in)_2.jpg`       | Marble built-in bathtub, chrome telephone mixer, blue art above  |
| `marble-tub-black-tap-vanity`| `odinary_tub(built-in).jpg`         | Marble built-in tub + white vanity with matte-black vessel tap   |
| `backlit-guest-toilet`       | `backlitGuestToilet.jpeg` (MISSING) | No photo on disk yet — item auto-hidden until the file is added  |

Notes
- Any image file in this folder is also included in the generic "send me your
  portfolio" gallery automatically — so dropping these here adds them to the
  portfolio as requested.
- Prices in `portfolio_catalog.py` are taken **verbatim from the pricing table
  in `bot/sales_profiles/homebase.md`** (the source of truth). If that table
  changes, update the `price` fields to match — never invent prices beyond it.
