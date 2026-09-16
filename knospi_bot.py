#!/usr/bin/env python3
"""
Knospi Instagram-Bot
=====================
Holt die Sensordaten von Knospi (Forellenbegonie / Begonia maculata),
bewertet sie mit denselben Schwellenwerten wie die Knospi-App und postet
bei bestimmten Ereignissen automatisch eine Instagram-Story:

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
    elif soil_state == "high":
        mood = "soggy"
    else:
        mood = "happy"

    return {"soil": soil_state, "temp": temp_state, "hum": hum_state, "mood": mood}


# Sprüche exakt im Ton der App (says-Logik aus applyLive), um Kälte/Wärme ergänzt
def says_for(reading, states):
    soil, temp, hum = reading["soil"], reading["temp"], reading["hum"]
    if states["soil"] == "low":
        return f"Meine Erde ist bei {soil:.0f} Prozent — unter 40 wird mir das zu trocken. Ein Schluck, bitte."
    if states["hum"] == "low":
        return f"Die Luft hier hat nur {hum:.0f} Prozent. Ich komme aus dem Regenwald, mir ist das zu trocken."
    if states["temp"] == "low":
        return f"Nur {temp:.1f} Grad hier — mir wird's langsam kalt. Unter 18 Grad mag ich gar nicht."
    if states["temp"] == "high":
        return f"{temp:.1f} Grad sind mir zu warm, dazu noch ohne Zugluft. Etwas kühler, bitte."
    if states["soil"] == "high":
        return f"Ganz schön nass hier unten — {soil:.0f} Prozent. Lass mich kurz abtrocknen."
    return f"Erde bei {soil:.0f} Prozent, Luft passt auch. Punkte sitzen, alles gut."


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
    if last_daily_iso is None:
        return True
    last = datetime.fromisoformat(last_daily_iso)
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


def post_story(image_url: str):
    ig_user_id = os.environ["IG_USER_ID"]
    access_token = os.environ["IG_ACCESS_TOKEN"]

    create_resp = requests.post(
        f"https://graph.facebook.com/{GRAPH_VERSION}/{ig_user_id}/media",
        data={
            "image_url": image_url,
            "media_type": "STORIES",
            "access_token": access_token,
        },
        timeout=30,
    )
    create_resp.raise_for_status()
    creation_id = create_resp.json()["id"]

    publish_resp = requests.post(
        f"https://graph.facebook.com/{GRAPH_VERSION}/{ig_user_id}/media_publish",
        data={"creation_id": creation_id, "access_token": access_token},
        timeout=30,
    )
    publish_resp.raise_for_status()
    return publish_resp.json()


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
            "text": f"Hallo, hier ist {PLANT_NAME}! " + says_for(reading, states),
            "label": "täglicher Hallo-Post",
        })

    for kind, key, old, new in events:
        label = {"soil": "Erdfeuchte", "temp": "Temperatur", "hum": "Luftfeuchte"}[key]
        if kind == "tip":
            to_post.append({
                "text": says_for(reading, states),
                "label": f"{label} kippt ({old} -> {new})",
            })
        else:
            to_post.append({
                "text": f"{label} passt wieder. " + says_for(reading, states),
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

        # Kurze Pause, damit raw.githubusercontent.com das neue Bild sicher ausliefert
        time.sleep(8)

        result = post_story(public_url)
        print(f"Instagram-Story gepostet: {result}")

        prev_state = new_state
        daily_due = False  # nur einmal pro Lauf als "Hallo" zählen


if __name__ == "__main__":
    main()
