# Where is this: live maps

The live path uses `runtime.py` for both HTTP and WebSocket actions. Host, player and spectator render the same geographic data through `static/js/where-map.js`.

## Map data

- Data root: `static/Where_is_this/maps/` (case preserved).
- `maps_config.json` defines geographic extents, filenames, colors and horizontal repetition.
- GeoJSON coordinates are WGS84 longitude/latitude. The supplied SVG files are previews and are not used for coordinate picking.
- World: Web Mercator, repeated horizontally. Europe: EPSG:3035 / Lambert Azimuthal Equal Area. Germany: EPSG:25832 / UTM 32N.
- Local Proj4js is pinned under `static/vendor/proj4/`, including its license and package integrity information. No external map service is required.
- Germany's three land-border components are separate SVG overlays so their stroke stays thin when zooming.
- Areas outside a regional map extent are gray; they are not presented as sea or accepted as guesses.
- Source attribution for the supplied map data remains in its original README.

## State and scoring

- Stored guesses use latitude/longitude and compatible normalized Web-Mercator coordinates, independently of the displayed projection.
- Distance is calculated on the server using the Haversine formula (Earth radius 6371 km).
- Each question round freezes its question, target, scoring configuration and participant roster.
- Ranking uses the whole round roster; equal distances are ordered by server submission time.
- New zone configurations use N equal-width rings scoring N down to 1, with zero outside. Existing legacy zone definitions are retained until a game is reconfigured.
- Drafts persist immediately. Confirmation locks a marker. Host reveal finalizes drafts once and awards points; read-only player/spectator requests never reveal a question themselves.
- Solutions, grades and other teams' markers remain hidden before the common reveal. Host snapshots are never published as public question data.
- Pause/resume retains the deadline, drafts and scores. Repeated questions have separate round identities.

## Automated coverage

`test_live.py` covers scoring, 25 concurrent HTTP submissions, capabilities, deadline/pause handling, replay isolation and public-data boundaries. `test_live_browser.py` covers Standard/VHS participants, native Hub routing, host reconnection, all three map projections, horizontal world wrapping, default-zoom reveal and spectator overlays. Its Germany screenshot assertion checks that land is visible and border strokes do not cover the map. `test_migration_live.py` preserves legacy scores, coordinates and unknown question slots.
