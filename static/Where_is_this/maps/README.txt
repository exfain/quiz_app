WHERE IS THIS? – KARTEN V2 (INSELN + OHNE BINNENGEWÄSSER)
=======================================================

Einsatz:
  Kopiere diese Dateien in static/where_is_this/maps/ (oder die vom Projekt verwendete Static-Struktur).
  .geojson = echte geografische Geometriedaten in WGS84 (Longitude, Latitude).
  .svg = skalierbare, fertig projizierte VORSCHAUEN, keine interaktiven Koordinatenkarten.

Änderungen zu V1:
  - Küstenpolygone in mittlerer GSHHG-Auflösung (intermediate) und geringere Mindestinselgrößen.
  - Nord-/Ostfriesische Inseln und zahlreiche weitere kleinere Inseln jetzt enthalten.
  - Keine zusätzlichen Polygone für Binnengewässer (Seen, Stauseen, wasserartige Flussstrukturen).
  - Wasser außerhalb der Küstenlinie bleibt blau; Land inklusive aller Nachbarländer grün.
  - Nur Deutschland-Grenze als eigene dünne schwarze Overlay-Linie.

WICHTIG:
  Die Karten sind bewusst stark reduziert. Auch große Binnen-SEEN werden derzeit grün dargestellt.
  Möchte man später einzelne große Seen zeigen, müssen diese gezielt als separate water-Features ergänzt werden.
  Die farbigen SVGs sind nur Vorschau. Für exakte Ortswahl in der App GeoJSON + Projektion verwenden.

Geodatenquelle:
  GSHHG intermediate resolution (GSHHG/GSHHS, über installierte Basemap-Geodaten).
  Deutschland-Staatsgrenze: vorhandene WDBII-Segmente aus V1.
  GSHHG Lizenz: LGPL; Lizenz-/Attributionspflichten bei Weitergabe prüfen.
  https://www.soest.hawaii.edu/wessel/gshhg/

GEOGRAPHISCHER HINWEIS:
  Das Öffnen der Karte und das Distanzscoring hängen nicht von der Kontur-Detaillierung ab:
  Immer Lon/Lat WGS84 speichern und Distanzen per Großkreis/Haversine berechnen.

V3 GRENZKORREKTUR:
  Deutschland-Grenzlayer stammt jetzt aus der geschlossenen Länderumriss-Geometrie
  von isellsoap/deutschlandGeoJSON (mittlere Auflösung, vereinfacht).
  Keine zusätzlichen Nachbarland-Grenzsegmente enthalten.
  Deutschlandumriss in 12 geschlossenen Ringen (Hauptland und größere Inseln).
  Quelle: https://github.com/isellsoap/deutschlandGeoJSON (Unlicense laut Repository).
  Die Kartenfarben, Land-/Wassergeometrien und weitere Karten bleiben unverändert.

V4 GRENZKORREKTUR – AUSSCHLIESSLICH LANDGRENZEN:
  germany_border.geojson zeigt ausschließlich drei Teilstücke der internationalen Landgrenzen:
  1. Niederlande über den Süden bis Polen, 2. Dänemark, 3. kurzer Grenzverlauf auf Usedom.
  Alle Meeresküsten, Inselumrisse und Grenzabschnitte in Lagunen sind entfernt.
  german.svg verwendet exakt dieselben drei Teilstücke.
  Farblayout, Koordinatenbezug, Welt- und Europakarte bleiben unverändert.
