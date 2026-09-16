# Knospi Instagram-Bot

Postet automatisch Instagram-Stories für Knospi (Forellenbegonie / Begonia
maculata), basierend auf den echten Sensordaten. Läuft komplett kostenlos
über GitHub Actions – ich (Claude) poste nicht selbst und halte auch keine
Zugangsdaten; du behältst die volle Kontrolle über Repo, Secrets und Account.

## Was der Bot macht

Alle 15 Minuten:
1. Ruft die Sensordaten ab.
2. Berechnet Erdfeuchte-, Temperatur- und Luftfeuchte-Zustand mit denselben
   Schwellen wie die Knospi-App (40–80 % Erde, 18–25 °C, 50–85 % Luft).
3. Vergleicht mit dem zuletzt gespeicherten Zustand (`state.json`) und postet:
   - **einmal täglich** ein "Hallo"
   - **sofort**, wenn ein Wert von ok → durstig/nass/kalt kippt
   - **sofort**, wenn ein Wert sich wieder normalisiert (z.B. nach dem Gießen)
4. Rendert dafür ein Story-Bild: die passende der sechs Original-Kacheln aus
   dem Artefakt (`assets/moods/*.png`, eine pro Stimmung) mit individuellem
   Text in der Sprechblase.
5. Committet das Bild ins Repo (damit es eine öffentliche Bild-URL hat) und
   postet es über die Instagram Graph API als Story.

## Einmalige Einrichtung

### 1. Instagram Business-/Creator-Konto + Facebook-Seite

Die Graph API funktioniert nur mit einem **Instagram Business- oder
Creator-Konto**, das mit einer **Facebook-Seite** verknüpft ist (kann eine
ganz einfache, unsichtbare Seite sein).

1. Instagram-App → Einstellungen → Konto → "Zu professionellem Konto wechseln"
   → "Unternehmen" (oder "Creator") wählen.
2. Eine Facebook-Seite erstellen (falls noch keine vorhanden): [facebook.com/pages/create](https://facebook.com/pages/create)
3. In den Instagram-Einstellungen unter "Verknüpfte Konten" die Facebook-Seite verbinden.

### 2. Meta-App anlegen und Access Token holen

1. Auf [developers.facebook.com](https://developers.facebook.com) einloggen, "Meine Apps" → "App erstellen" → Typ "Andere" / "Business".
2. Im App-Dashboard das Produkt **"Instagram Graph API"** hinzufügen.
3. Im [Graph API Explorer](https://developers.facebook.com/tools/explorer/) deine App auswählen, unter "Berechtigungen" mindestens hinzufügen:
   - `instagram_basic`
   - `instagram_content_publish`
   - `pages_show_list`
   - `pages_read_engagement`
4. Access Token generieren, damit die eigene Facebook-Seite und das
   verknüpfte Instagram-Konto auswählen/bestätigen.
5. Diesen **kurzlebigen** Token in einen **langlebigen** Token umwandeln (60 Tage gültig):
   ```
   https://graph.facebook.com/v21.0/oauth/access_token
     ?grant_type=fb_exchange_token
     &client_id=DEINE_APP_ID
     &client_secret=DEIN_APP_SECRET
     &fb_exchange_token=DEIN_KURZER_TOKEN
   ```
6. Deine `IG_USER_ID` herausfinden:
   ```
   https://graph.facebook.com/v21.0/me/accounts?access_token=DEIN_TOKEN
   ```
   → liefert deine Page-ID, damit dann:
   ```
   https://graph.facebook.com/v21.0/DEINE_PAGE_ID?fields=instagram_business_account&access_token=DEIN_TOKEN
   ```
   → das ist deine `IG_USER_ID`.

**Wichtig:** Der langlebige Token läuft nach 60 Tagen ab. Du musst ihn
regelmäßig erneuern (Schritt 5) und in GitHub aktualisieren, oder später
einen Refresh-Mechanismus ergänzen. Für den Start reicht das manuelle
Erneuern alle ~2 Monate.

### 3. Eigenes GitHub-Repo anlegen

1. Ein **öffentliches** Repo erstellen (muss öffentlich sein, damit
   `raw.githubusercontent.com` die Bilder ohne Login ausliefert).
2. Diese Dateien hochladen (kompletter Inhalt dieses Ordners).
3. Unter **Settings → Secrets and variables → Actions → New repository secret**:
   - `IG_USER_ID` = deine Instagram-User-ID aus Schritt 2.6
   - `IG_ACCESS_TOKEN` = dein langlebiger Access Token aus Schritt 2.5
4. Unter **Settings → Actions → General → Workflow permissions**:
   "Read and write permissions" aktivieren (damit der Bot Bilder committen darf).

### 4. Schriftart ergänzen (optional, für 1:1-Look)

Das Artefakt nutzt die Schrift "Baloo 2" für die Sprechblasen-Texte. Lade
`Baloo2-Bold.ttf` (z.B. von Google Fonts) herunter und lege sie unter
`assets/fonts/Baloo2-Bold.ttf` ab — dann sieht der Text noch näher am
Original aus. Ohne diese Datei nutzt das Skript automatisch eine
Systemschrift als Fallback.

### Neue Stimmung / neue Vorlage ergänzen

Falls ihr später weitere Pflanzen oder Zustände ergänzt: einfach eine neue
PNG (1080×1080, gleiche Sprechblasen-Position wie die bestehenden sechs)
unter `assets/moods/<name>.png` ablegen und in `MOOD_TEMPLATE` in
`knospi_bot.py` eintragen.

### 5. Testen

Manuell auslösen über **Actions → Knospi Instagram Bot → Run workflow** —
einmal mit "dry_run: true" testen (rendert nur, postet nicht), dann live.

## Grenzen, die du kennen solltest

- Instagram/Meta erlaubt maximal ~25 API-Posts pro Tag und Konto.
- Die Erdfeuchte-Schwellen sind laut Artefakt selbst "noch nicht kalibriert"
  — es kann also anfangs zu falschen "kippt"-Meldungen kommen. Passe
  `SOIL_LOW`/`SOIL_HIGH` in `knospi_bot.py` an, sobald ihr bessere
  Referenzwerte (trockene vs. frisch gegossene Erde) habt.
- Läuft der Sensor mal nicht (kein neuer Messwert), postet der Bot einfach
  nichts Neues — es gibt keine Fehler-Story.
