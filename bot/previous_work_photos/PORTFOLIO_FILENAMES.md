# Portfolio image filenames

The bot can send individual portfolio pieces with their own title, price and
back-story. Metadata lives in `bot/portfolio_catalog.py`; the images must live
in this folder under the **exact filenames** below.

A catalogue item only becomes "sendable" once its file is present here
(`portfolio_catalog.available_items()` checks the disk), so the bot never
promises a photo it can't actually send.

Save each attached image under the matching name:

| Filename                       | Which photo to save here                                              | Status        |
|--------------------------------|-----------------------------------------------------------------------|---------------|
| `modernKitchenIsland.jpeg`     | Modern grey/white kitchen, white quartz island, pendant lights        | NEEDS FILE    |
| `navyShakerKitchen.jpeg`       | Navy-blue shaker kitchen, gas hob on island, patterned tile floor     | NEEDS FILE    |
| `freestandingTubHexBorder.jpeg`| Oval white freestanding tub, black tap, hex mosaic border, wall-hung WC| NEEDS FILE   |
| `doubleVanity.jpeg`            | Gold square taps, twin white basins, plumber in orange vest           | ALREADY ON DISK |
| `blackGraniteVanityTub.jpeg`   | Black floating granite vanity + sculpted black freestanding tub       | NEEDS FILE    |
| `backlitGuestToilet.jpeg`      | Narrow guest WC with backlit stone feature wall                       | NEEDS FILE    |
| `classicToiletBasinSuite.jpeg` | Simple close-coupled toilet + pedestal basin, beige tiles             | NEEDS FILE    |
| `clawfootTubFeatureWall.jpeg`  | White clawfoot/roll-top tub, brick-effect feature wall, wall-hung WC  | NEEDS FILE    |
| `walkInRainShower.jpeg`        | Frameless glass walk-in rain shower, mosaic feature stripe            | NEEDS FILE    |
| `marbleBuiltInTub.jpeg`        | Marble built-in bathtub, chrome telephone mixer, blue art above       | NEEDS FILE    |
| `marbleTubBlackTapVanity.jpeg` | Marble built-in tub + white vanity with matte-black vessel tap        | NEEDS FILE    |

Notes
- Any image file in this folder is also included in the generic "send me your
  portfolio" gallery automatically — so dropping these here adds them to the
  portfolio as requested.
- Prices in `portfolio_catalog.py` are taken **verbatim from the pricing table
  in `bot/sales_profiles/homebase.md`** (the source of truth). If that table
  changes, update the `price` fields to match — never invent prices beyond it.
