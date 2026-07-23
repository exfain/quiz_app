# Where is this? world map validation

Manual checks for the static Web-Mercator world map implementation.

## Map rendering
- Open a Where-is-this player page for an active question.
- Confirm the world map is loaded from `where_is_this/maps/world_web_mercator_blank_landmasses.svg`.
- Confirm water is blue and land is green.
- Confirm the SVG remains sharp with browser zoom.
- Resize the browser and confirm existing markers remain on the same visual location.
- Confirm no JavaScript console errors are emitted by the Where-is-this page.

## Player click and reveal
- Click Berlin, Paris, New York, Sydney, Tokyo, and Cape Town positions on the world map.
- Submit at least one answer and confirm the answer is stored as normalized coordinates.
- End the question and confirm the reveal shows player marker, correct marker, distance, and points.
- In Ranking mode, confirm the reveal shows the participant rank.
- In Zielzonen mode, confirm the reveal shows the matched zone or outside-zone state.

## Host and management pages
- Open the Where-is-this management page and create a question with `Weltkarte`.
- Confirm the correct-location preview uses the same static SVG map.
- Confirm the host monitor shows the selected scoring mode.
- Confirm the host preview uses the static SVG and no map tiles.

## Offline resource check
- Confirm the browser network panel shows no Leaflet, OpenStreetMap, Carto, Google Maps, external tile, or external marker requests for Where-is-this.
- Confirm distance and scoring continue to work with network access disabled after the app page has loaded.

## Runtime/rejoin checks
- Rejoin as a player during an active question before submitting.
- Rejoin as a player after submitting and before reveal.
- Rejoin as a player after reveal.
- Rejoin as host during an active question and after reveal.
- Confirm no duplicate answers are created and scorebox entries remain stable.
