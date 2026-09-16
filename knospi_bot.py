#!/usr/bin/env python3
"""
Knospi Instagram-Bot
=====================
Holt die Sensordaten von Knospi (Forellenbegonie / Begonia maculata),
bewertet sie mit denselben Schwellenwerten wie die Knospi-App und postet
bei bestimmten Ereignissen automatisch einen Instagram-Feed-Beitrag:

  1. Einmal am Tag ein "Hallo" (unabhängig vom Zustand)
  2. Immer wenn ein Wert von "ok" nach "low"/"high" kippt
  3. Immer wenn ein Wert von "low"/"high" zurück nach "ok" springt (z.B. nach dem Gießen)

Schwellenwerte 1:1 aus dem Artefakt übernommen (Stand 09/2026, Richtwerte
für Begonia maculata, von der App selbst als "vorläufig, nicht kalibriert"
markiert):

    Erdfeuchte:   40 - 80 %
    Luftfeuchte:  50 - 85 %
    Temperatur:   18 - 25 °C
    Licht:        wird nicht bewertet (Sensor liefert nur Rohwerte, keine Lux)
"""

import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

SENSOR_URL = "https://sensors.duus.digital/f7c25fe9cc3670bea6aeabe3090ed78647e889067f73fd46a548c7d37ec8b44b/tail?n=20"

# Exakt dieselben Schwellen wie in der Knospi-App (SOIL_DRY/SOIL_WET + band()-Aufrufe)
SOIL_LOW, SOIL_HIGH = 40, 80
TEMP_LOW, TEMP_HIGH = 18, 25
HUM_LOW, HUM_HIGH = 50, 85

# Berlin-Zeit für den "einmal am Tag"-Post
LOCAL_TZ = timezone(timedelta(hours=2))  # Sommerzeit; im Winter auf +1 anpassen oder zoneinfo nutzen

STATE_PATH = Path(__file__).parent / "state.json"
OUTPUT_DIR = Path(__file__).parent / "output"
FONT_DIR = Path(__file__).parent / "assets" / "fonts"
MOODS_DIR = Path(__file__).parent / "assets" / "moods"

PLANT_NAME = "Knospi"
SPECIES = "Begonia maculata"
SPECIES_SHORT = "Forellenbegonie"

# Ink-Farbe exakt aus den Vorlagen abgemessen (Umrandung + Text der Sprechblase)
INK = (29, 36, 24)

# Eine fertige Kachel-Grafik pro Stimmung (von dir aus dem Artefakt exportiert).
# Alle sechs haben exakt dieselbe Sprechblasen-Position (1080x1080, vermessen):
#   Rahmen außen: x 130-949, y 68-276 (Rechteck, ohne die Spitze nach unten)
#   Füllung innen: x 136-944, y 74-270
MOOD_TEMPLATE = {
    "happy": MOODS_DIR / "happy.png",
    "thirsty": MOODS_DIR / "thirsty.png",
    "soggy": MOODS_DIR / "soggy.png",
    "dark": MOODS_DIR / "dark.png",
    "cold": MOODS_DIR / "cold.png",
    "air": MOODS_DIR / "air.png",
}
BUBBLE_BOX = (170, 95, 910, 260)  # x0,y0,x1,y1 - Text-sicherer Bereich innerhalb der Füllung

# ---------------------------------------------------------------------------
# Sensor-Auswertung (Portierung der band()/buildLive()/applyLive()-Logik der App)
# ---------------------------------------------------------------------------

def band(value, lo, hi):
    if value is None:
        return "na"
    if value < lo:
        return "low"
    if value > hi:
        return "high"
    return "ok"


def fetch_latest_reading():
    resp = requests.get(SENSOR_URL, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    messages = data.get("messages", [])
    rows = [
        m for m in messages
        if m.get("json", {}).get("soil", {}).get("percent") is not None
    ]
    if not rows:
        raise RuntimeError("Keine gültigen Messwerte im Sensor-Feed gefunden")
    last = rows[-1]["json"]
    return {
        "ts": rows[-1]["ts"],
        "soil": last["soil"]["percent"],
        "soil_raw": last["soil"].get("raw"),
        "temp": last.get("climate", {}).get("temperature_c"),
        "hum": last.get("climate", {}).get("humidity_pct"),
        "light_visible": last.get("light", {}).get("visible"),
    }


def evaluate(reading):
    soil_state = band(reading["soil"], SOIL_LOW, SOIL_HIGH)
    temp_state = band(reading["temp"], TEMP_LOW, TEMP_HIGH)
    hum_state = band(reading["hum"], HUM_LOW, HUM_HIGH)

    if soil_state == "low":
        mood = "thirsty"
    elif hum_state == "low":
        mood = "air"
    elif temp_state == "low":
        mood = "cold"
    elif temp_state == "high":
        mood = "cold"
    elif soil_state == "high" or hum_state == "high":
        mood = "soggy"
    else:
        mood = "happy"

    return {"soil": soil_state, "temp": temp_state, "hum": hum_state, "mood": mood}


# ---------------------------------------------------------------------------
# Textbausteine: mehrere lustige Varianten pro Situation, damit nicht jeden
# Tag derselbe Satz kommt. random.choice() pickt jedes Mal neu.
# Alle Platzhalter ({soil}, {temp}, {hum}) werden mit den echten Messwerten
# gefüllt, damit die Sprüche trotzdem informativ bleiben.
# ---------------------------------------------------------------------------

HELLO_TEMPLATES = [
    "Hallo, hier ist {name}! Erde bei {soil:.0f}%, {temp:.1f}°C, Luft {hum:.0f}% — reicht mir gerade völlig zum Angeben.",
    "Guten Tag! {name} meldet sich aus der Ecke: {soil:.0f}% Erdfeuchte, alles im grünen Bereich, die silbrigen Punkte glänzen heute besonders.",
    "Psst, {name} hier. Falls du dich fragst: {temp:.1f}°C, {hum:.0f}% Luftfeuchte — perfekt für ein Nickerchen im Topf.",
    "{name} reportet vom Fensterbrett: Erde {soil:.0f}%, alles ruhig, keine Dramen heute.",
    "Ein Hallo von {name}! Ich stehe hier bei {temp:.1f}°C rum und fühle mich, ehrlich gesagt, ziemlich gut.",
    "Tagesmeldung von {name}: {soil:.0f}% Erde, {hum:.0f}% Luft — genau mein Geschmack.",
    "Hi, {name} hier — ein kleiner Gruß zwischendurch. Meine Werte? {soil:.0f}% Erde, {temp:.1f}°C. Business as usual.",
    "Guten Morgen von {name}! Kein Grund zur Sorge heute, nur ein freundliches Hallo aus dem Topf.",
    "{name} checkt ein: {soil:.0f}% Erde, {temp:.1f}°C. Ein ganz normaler Tag im Leben einer Forellenbegonie.",
    "Klopf klopf, {name} hier. Nichts Aufregendes, nur {hum:.0f}% Luftfeuchte und gute Laune.",
]

TIP_TEMPLATES = {
    "soil_low": [
        "Meine Erde ist bei {soil:.0f}% — unter 40 wird mir das zu trocken. Ein Schluck, bitte.",
        "{soil:.0f}% Erdfeuchte und sinkend. Ich fange an, mich wie eine Rosine zu fühlen.",
        "Hallo? Durst hier! {soil:.0f}% Erde ist mir zu wenig fürs Wohlbefinden.",
        "Bin bei {soil:.0f}% Erdfeuchte — das ist Wüsten-Niveau für eine Regenwaldpflanze wie mich.",
        "{soil:.0f}% Erde. Ich hänge schon leicht durch. Gießkanne, bitte melden.",
    ],
    "soil_high": [
        "Ganz schön nass hier unten — {soil:.0f}%. Lass mich kurz abtrocknen.",
        "{soil:.0f}% Erdfeuchte, meine Wurzeln machen schon Schwimmunterricht. Bitte eine Gießpause.",
        "Zu viel des Guten: {soil:.0f}% nasse Erde. Ich brauche keinen Pool, danke.",
        "Bei {soil:.0f}% Erdfeuchte fühle ich mich wie im Sumpf. Kurz trocknen lassen, bitte.",
    ],
    "temp_low": [
        "Nur {temp:.1f} Grad hier — mir wird's langsam kalt. Unter 18 Grad mag ich gar nicht.",
        "{temp:.1f} Grad?! Wo ist meine Decke. Ich komme aus Brasilien, nicht aus der Antarktis.",
        "Brr, {temp:.1f} Grad. Ich fröstel hier leise vor mich hin.",
        "{temp:.1f} Grad sind mir zu frisch. Ein Platz ohne Zugluft wäre jetzt fein.",
    ],
    "temp_high": [
        "{temp:.1f} Grad sind mir zu warm, dazu noch ohne Zugluft. Etwas kühler, bitte.",
        "Puh, {temp:.1f} Grad — mir wird ganz schwül unter den Blättern.",
        "Bei {temp:.1f} Grad brauche ich glatt einen kleinen Fächer.",
    ],
    "hum_low": [
        "Die Luft hier hat nur {hum:.0f}%. Ich komme aus dem Regenwald, mir ist das zu trocken.",
        "{hum:.0f}% Luftfeuchte — meine Blätter fühlen sich an wie Chips.",
        "Trockene Luft bei {hum:.0f}%. Ein Luftbefeuchter wäre jetzt mein bester Freund.",
        "Nur {hum:.0f}% Luftfeuchte. Ich vermisse mein feuchtes Zuhause im Regenwald.",
    ],
    "hum_high": [
        "{hum:.0f}% Luftfeuchte — es fühlt sich an wie Dschungel-Dampfsauna hier drin.",
        "So feucht bei {hum:.0f}%! Fast schon zu viel Regenwald-Feeling für mich.",
    ],
}

IMPROVE_TEMPLATES = {
    "soil": [
        "Erdfeuchte passt wieder ({soil:.0f}%). Danke fürs Gießen, ich fühl mich gleich lebendiger.",
        "Ah, {soil:.0f}% Erde — genau richtig. Krise abgewendet.",
        "{soil:.0f}% Erdfeuchte, wieder im grünen Bereich. Das war knapp.",
    ],
    "temp": [
        "{temp:.1f} Grad — wieder angenehm. Danke fürs Umstellen bzw. Lüften.",
        "Temperatur passt jetzt ({temp:.1f}°C). Ich taue innerlich auf.",
        "{temp:.1f} Grad, wieder im Wohlfühlbereich. Sehr angenehm.",
    ],
    "hum": [
        "Luftfeuchte wieder bei {hum:.0f}% — atmet sich gleich besser.",
        "{hum:.0f}% Luftfeuchte, genau mein Wohlfühlbereich. Danke!",
        "Endlich wieder {hum:.0f}% Luftfeuchte. Meine Blätter sagen Danke.",
    ],
}


def hello_message(reading):
    return random.choice(HELLO_TEMPLATES).format(name=PLANT_NAME, **reading)


def tip_message(reading, key, new_state):
    templates = TIP_TEMPLATES.get(f"{key}_{new_state}")
    if not templates:
        return f"{key} kippt nach {new_state} ({reading.get(key)})."
    return random.choice(templates).format(**reading)


def improve_message(reading, key):
    templates = IMPROVE_TEMPLATES.get(key, [])
    if not templates:
        return f"{key} ist wieder okay."
    return random.choice(templates).format(**reading)


MOOD_LABEL = {
    "happy": "Rundum wohl",
    "thirsty": "Durstig",
    "soggy": "Nasse Füße",
    "dark": "Sitzt im Dunkeln",
    "cold": "Fröstelt",
    "air": "Trockene Luft",
}

# ---------------------------------------------------------------------------
# State (letzter bekannter Zustand + letztes "Hallo"-Datum)
# ---------------------------------------------------------------------------

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"soil": "ok", "temp": "ok", "hum": "ok", "last_daily": None}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def decide_events(prev_state, new_states):
    """Liefert eine Liste von ('tip'|'improve', key, from_state, to_state)."""
    events = []
    for key in ("soil", "temp", "hum"):
        old, new = prev_state.get(key, "ok"), new_states[key]
        if old == new:
            continue
        if new in ("low", "high") and old == "ok":
            events.append(("tip", key, old, new))
        elif new == "ok" and old in ("low", "high"):
            events.append(("improve", key, old, new))
        elif old in ("low", "high") and new in ("low", "high"):
            # z.B. von zu trocken auf zu nass gesprungen -> zählt auch als "kippt"
            events.append(("tip", key, old, new))
    return events


def is_new_day(last_daily_iso):
    now_local = datetime.now(LOCAL_TZ)
    if not last_daily_iso:
        return True
    try:
        last = datetime.fromisoformat(last_daily_iso)
    except (ValueError, TypeError):
        # Ungültiger/kaputter Wert in state.json -> sicherheitshalber wie
        # "noch nicht heute gepostet" behandeln, statt den Lauf abzubrechen.
        return True
    return last.astimezone(LOCAL_TZ).date() < now_local.date()


# ---------------------------------------------------------------------------
# Bild-Generierung: fertige Artefakt-Kachel + individueller Text in der Sprechblase
# ---------------------------------------------------------------------------

def load_font(size, bold=True):
    """Versucht eine passende Schrift zu laden, fällt sonst auf Standard zurück.
    Lege eine eigene .ttf (z.B. Baloo 2 oder Nunito) unter assets/fonts/ ab,
    damit der Text exakt zur Beschriftung der Vorlagen passt."""
    candidates = [
        FONT_DIR / ("Baloo2-Bold.ttf" if bold else "Baloo2-Medium.ttf"),
        FONT_DIR / ("Nunito-Bold.ttf" if bold else "Nunito-SemiBold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for c in candidates:
        if c.exists():
            try:
                return ImageFont.truetype(str(c), size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap_to_width(draw, text, font, max_width):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit_text(draw, text, box, start_size=52, min_size=26, line_gap=1.28):
    """Sucht die größte Schriftgröße, bei der der Text noch in die Box passt."""
    x0, y0, x1, y1 = box
    max_w, max_h = x1 - x0, y1 - y0
    size = start_size
    while size >= min_size:
        font = load_font(size)
        lines = _wrap_to_width(draw, text, font, max_w)
        bbox = draw.textbbox((0, 0), "Ägy", font=font)
        line_h = int((bbox[3] - bbox[1]) * line_gap)
        total_h = line_h * len(lines)
        if total_h <= max_h:
            return font, lines, line_h
        size -= 2
    return font, lines, line_h  # kleinste Größe, auch wenn's knapp wird


def render_story(reading, states, body_text):
    mood = states["mood"]
    template_path = MOOD_TEMPLATE.get(mood, MOOD_TEMPLATE["happy"])
    img = Image.open(template_path).convert("RGB")
    draw = ImageDraw.Draw(img)

    font, lines, line_h = _fit_text(draw, body_text, BUBBLE_BOX)

    x0, y0, x1, y1 = BUBBLE_BOX
    total_h = line_h * len(lines)
    y = y0 + ((y1 - y0) - total_h) // 2
    cx = (x0 + x1) // 2

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        w = bbox[2] - bbox[0]
        draw.text((cx - w // 2, y), line, font=font, fill=INK)
        y += line_h

    OUTPUT_DIR.mkdir(exist_ok=True)
    filename = f"story_{mood}_{int(time.time() * 1000)}.png"
    out_path = OUTPUT_DIR / filename
    img.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------------------
# GitHub: Bild committen, damit es unter einer öffentlichen raw.githubusercontent-URL
# erreichbar ist (die Instagram Graph API braucht eine öffentliche Bild-URL, keinen Upload)
# ---------------------------------------------------------------------------

def commit_and_get_public_url(image_path: Path):
    repo = os.environ["GITHUB_REPOSITORY"]  # z.B. "meinuser/knospi-bot"
    branch = os.environ.get("GITHUB_REF_NAME", "main")

    subprocess.run(["git", "config", "user.name", "knospi-bot"], check=True)
    subprocess.run(["git", "config", "user.email", "knospi-bot@users.noreply.github.com"], check=True)
    subprocess.run(["git", "add", str(image_path), str(STATE_PATH)], check=True)
    subprocess.run(["git", "commit", "-m", f"post: {image_path.name}"], check=True)
    subprocess.run(["git", "push"], check=True)

    rel_path = image_path.relative_to(Path(__file__).parent)
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{rel_path.as_posix()}"


# ---------------------------------------------------------------------------
# Instagram Graph API
# ---------------------------------------------------------------------------

GRAPH_VERSION = "v21.0"


def post_feed_image(image_url: str, caption: str = ""):
    """Veröffentlicht einen normalen Feed-Beitrag (kein Story).
    media_type wird bewusst weggelassen -> Graph API Standardwert ist IMAGE."""
    ig_user_id = os.environ["IG_USER_ID"]
    access_token = os.environ["IG_ACCESS_TOKEN"]

    create_resp = requests.post(
        f"https://graph.facebook.com/{GRAPH_VERSION}/{ig_user_id}/media",
        data={
            "image_url": image_url,
            "caption": caption,
            "access_token": access_token,
        },
        timeout=30,
    )
    if not create_resp.ok:
        print("Fehlerantwort beim Erstellen des Media-Containers:", create_resp.text)
    create_resp.raise_for_status()
    creation_id = create_resp.json()["id"]

    # Instagram lädt und verarbeitet das Bild von image_url asynchron im
    # Hintergrund. Vor dem Veröffentlichen muss der Container den Status
    # "FINISHED" erreichen, sonst schlägt media_publish mit 400 fehl.
    status = _wait_for_container_ready(ig_user_id, creation_id, access_token)
    print(f"Media-Container-Status: {status}")

    publish_resp = requests.post(
        f"https://graph.facebook.com/{GRAPH_VERSION}/{ig_user_id}/media_publish",
        data={"creation_id": creation_id, "access_token": access_token},
        timeout=30,
    )
    if not publish_resp.ok:
        print("Fehlerantwort beim Veröffentlichen:", publish_resp.text)
    publish_resp.raise_for_status()
    return publish_resp.json()


def _wait_for_container_ready(ig_user_id, creation_id, access_token, max_attempts=15, delay_seconds=4):
    """Fragt den Verarbeitungsstatus des Media-Containers ab, bis er
    FINISHED ist (oder gibt nach max_attempts auf / bricht bei ERROR ab)."""
    for attempt in range(1, max_attempts + 1):
        resp = requests.get(
            f"https://graph.facebook.com/{GRAPH_VERSION}/{creation_id}",
            params={"fields": "status_code,status", "access_token": access_token},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        status_code = data.get("status_code")
        if status_code == "FINISHED":
            return status_code
        if status_code == "ERROR":
            raise RuntimeError(f"Instagram konnte das Bild nicht verarbeiten: {data}")
        print(f"Warte auf Bildverarbeitung durch Instagram ({attempt}/{max_attempts}): {data}")
        time.sleep(delay_seconds)
    raise TimeoutError("Media-Container wurde nach mehreren Versuchen nicht FINISHED.")


# ---------------------------------------------------------------------------
# Hauptlogik
# ---------------------------------------------------------------------------

def main():
    dry_run = "--dry-run" in sys.argv

    reading = fetch_latest_reading()
    states = evaluate(reading)
    prev_state = load_state()

    events = decide_events(prev_state, states)
    daily_due = is_new_day(prev_state.get("last_daily"))

    if not events and not daily_due:
        print("Keine Veränderung und kein täglicher Post fällig – nichts zu tun.")
        return

    to_post = []

    if daily_due:
        to_post.append({
            "text": hello_message(reading),
            "label": "täglicher Hallo-Post",
        })

    for kind, key, old, new in events:
        label = {"soil": "Erdfeuchte", "temp": "Temperatur", "hum": "Luftfeuchte"}[key]
        if kind == "tip":
            to_post.append({
                "text": tip_message(reading, key, new),
                "label": f"{label} kippt ({old} -> {new})",
            })
        else:
            to_post.append({
                "text": improve_message(reading, key),
                "label": f"{label} verbessert ({old} -> {new})",
            })

    for post in to_post:
        image_path = render_story(reading, states, post["text"])
        print(f"Story gerendert: {image_path} — {post['label']}")

        if dry_run:
            print("(--dry-run) Kein echter Post, kein Commit.")
            continue

        # Zustand VOR dem Commit aktualisieren, damit state.json mit hochgeladen wird
        new_state = {"soil": states["soil"], "temp": states["temp"], "hum": states["hum"],
                     "last_daily": datetime.now(LOCAL_TZ).isoformat() if daily_due else prev_state.get("last_daily")}
        save_state(new_state)

        public_url = commit_and_get_public_url(image_path)
        print(f"Öffentliche Bild-URL: {public_url}")

        # Pause, damit raw.githubusercontent.com das neue Bild sicher ausliefert,
        # bevor Instagram versucht, es abzurufen.
        time.sleep(15)

        caption = (
            f"{post['text']}\n\n"
            f"🌿 {PLANT_NAME} · {SPECIES_SHORT}\n"
            f"Erde {reading['soil']:.0f}% · Luft {reading['hum']:.0f}% · {reading['temp']:.1f}°C"
        )
        result = post_feed_image(public_url, caption)
        print(f"Instagram-Beitrag gepostet: {result}")

        prev_state = new_state
        daily_due = False  # nur einmal pro Lauf als "Hallo" zählen


if __name__ == "__main__":
    main()
