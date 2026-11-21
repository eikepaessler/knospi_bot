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
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

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

# Wie in der App: Zu trockene Luft ist nur noch ein Hinweis, kein Problem.
# Knospi guckt dann freundlich, es gibt keinen "wird schlecht"-/"Danke"-Post
# dazu und sie zählt nicht als "braucht dich". Im täglichen Hallo-Post erwähnt
# sie die trockene Luft aber kurz und entspannt. Auf True stellen, wenn trockene
# Luft wieder als echtes Problem gelten soll.
HUM_LOW_IS_PROBLEM = False

# Berlin-Zeit für den "einmal am Tag"-Post. ZoneInfo statt fixem UTC-Offset,
# damit die Sommer-/Winterzeit-Umstellung im Oktober automatisch berücksichtigt wird.
LOCAL_TZ = ZoneInfo("Europe/Berlin")

# Der tägliche "Hallo"-Post soll nicht zu einer beliebigen Uhrzeit kommen,
# sondern nur, wenn ein Workflow-Lauf in dieses lokale Zeitfenster fällt.
# Achtung: GitHub drosselt den Cron-Trigger auf wenig aktiven Repos teils auf
# mehrere Stunden Abstand statt der eingestellten 15 Minuten. Fällt an einem
# Tag kein Lauf in dieses Fenster, entfällt der Hallo-Post für diesen Tag
# ersatzlos. Bei Bedarf hier einfach breiter stellen (z.B. 13:00-15:00).
DAILY_WINDOW_START = dtime(8, 0)
DAILY_WINDOW_END = dtime(16, 0)

# Puffer gegen "Flackern" an der Grenze: Ist ein Wert einmal zu niedrig/hoch,
# gilt er erst wieder als "ok", wenn er um diesen Abstand klar im grünen
# Bereich liegt (z.B. Erde zu trocken bei <40% -> wieder ok erst ab 43%).
SOIL_MARGIN = 3     # Prozentpunkte
HUM_MARGIN = 3      # Prozentpunkte
TEMP_MARGIN = 1.0   # Grad Celsius

# Mindestabstand, bevor für denselben Wert erneut ein "wird schlecht"-Post
# kommt. Das "Danke, wieder besser" kommt immer sofort. Dass es ihr weiter
# schlecht geht, sagt der tägliche Hallo-Post (einmal pro Tag).
TIP_COOLDOWN_HOURS = 6

# ---------------------------------------------------------------------------
# Redaktionelle Regeln (aus dem Instagram-Briefing)
# ---------------------------------------------------------------------------

# Kein automatischer Post zwischen 22:00 und 07:00 Uhr (Berlin-Zeit).
QUIET_START = dtime(22, 0)
QUIET_END = dtime(7, 0)

# Automatische Sensor-Posts (Kippt / Danke / Hallo bei Problem): höchstens 1 pro Tag.
# Freigegebene Pool-Posts (siehe unten) zählen dabei nicht mit, sie sind ja von Hand freigegeben.
MAX_AUTOMATED_POSTS_PER_DAY = 1

# "Es wird schlecht"-Posts: höchstens 2 pro Woche, und pro Zustand ein Mindestabstand in Tagen
# (trockene Erde 1x pro Woche, zu nass 1x pro Monat). "Danke"-Posts sind davon ausgenommen.
MAX_TIPS_PER_WEEK = 2
TIP_MIN_DAYS_BETWEEN = {
    "soil_low": 7, "soil_high": 30,
    "temp_low": 7, "temp_high": 7,
    "hum_low": 7, "hum_high": 30,
}
# Außergewöhnlicher Anlass: Dann gelten die Wochen-/Monatslimits nicht (Tageslimit und Ruhezeit schon).
EXTRAORDINARY = {
    "soil_low": lambda v: v < 25,
    "soil_high": lambda v: v > 90,
    "temp_low": lambda v: v < 12,
    "temp_high": lambda v: v > 30,
    "hum_high": lambda v: v > 95,
}

# Ein einzelner Ausreißer löst nichts aus: Die letzten N Messungen müssen denselben Zustand zeigen.
STABLE_READINGS = 3
# Messwerte, die älter sind, gelten als veraltet -> keine Sensor-Posts.
MAX_READING_AGE_HOURS = 6

# Wie lange das Protokoll der Posts in state.json aufgehoben wird.
POST_LOG_KEEP_DAYS = 45

# --- Inhalts-Pool (vorbereitete Posts, ohne Sensor) -------------------------
# Entwürfe entstehen an diesen Wochentagen (0 = Montag ... 6 = Sonntag) zwischen 10 und 18 Uhr,
# höchstens POOL_MAX_PER_WEEK pro Woche. Sie gehen erst raus, wenn du sie freigibst.
POOL_WEEKDAYS = {0, 2, 3, 6}
POOL_MAX_PER_WEEK = 4
POOL_DRAFT_START = dtime(10, 0)
POOL_DRAFT_END = dtime(18, 0)
POOL_REPEAT_DAYS = 120      # derselbe Inhalt frühestens nach so vielen Tagen wieder
DRAFT_REVIEW_DAYS = 3       # Entwürfe, auf die du nicht antwortest, verfallen nach so vielen Tagen
DRAFT_EXPIRY_DAYS = 7       # freigegebene, aber (z.B. wegen Tageslimit) nicht gepostete Entwürfe verfallen danach
# Wunschverteilung der Inhalte (Startverteilung aus dem Briefing)
PILLAR_TARGETS = {"entertainment": 0.35, "wissen": 0.25, "community": 0.20, "app": 0.20}
PILLAR_LABEL = {
    "entertainment": "Entertainment", "wissen": "Pflanzenwissen",
    "community": "Community", "app": "App & Sensor",
}
# Ungefähr jeder dritte Pool-Post bekommt einen sanften Aufruf (CTA), falls der Inhalt einen hat.
CTA_EVERY = 3

FORMAT_LABEL = {
    "knospi_klaert_das": "Knospi klärt das",
    "pflanzenbeichte": "Pflanzenbeichte",
    "mythos": "Mythos der Woche",
    "knospi_news": "Knospi News",
    "pflanzenmoment": "Pflanzenmoment",
    "frag_knospi": "Frag Knospi",
    "wg_meeting": "WG-Meeting",
    "wg_comic": "Pflanzen-WG",
    "behind_the_scenes": "Behind the Scenes",
    "geschenk": "Verschenk-Idee",
    "community": "Community",
    "giessrunde": "Gießrunde",
}
CHARACTERS = {"knospi", "moni", "pepe", "siggi", "cleo"}
EXPRESSIONS = {
    "froh", "lacht", "zwinkert", "verliebt", "ueberrascht",
    "skeptisch", "genervt", "durstig", "traurig", "schlaeft",
}
SEASONS = {"spring", "summer", "autumn", "winter"}

STATE_PATH = Path(__file__).parent / "state.json"
POOL_PATH = Path(__file__).parent / "content_pool.json"
DRAFTS_DIR = Path(__file__).parent / "drafts"
CHAR_DIR = Path(__file__).parent / "assets" / "characters"
# Figuren werden an mehreren möglichen Orten gesucht (erster Treffer gewinnt).
# Dateiname: <figur>-<ausdruck>.png (oder <ausdruck>.png), z.B. moni-durstig.png
_ROOT = Path(__file__).parent
CHAR_SEARCH_DIRS = [
    "assets/characters/{c}",
    "marketing/figuren/png/{c}",
    "marketing/figuren/figuren/png/{c}",
    "marketing/figuren/{c}",
    "figuren/png/{c}",
]
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
BUBBLE_BOX = (160, 130, 920, 340)  # x0,y0,x1,y1 - Text-sicherer Bereich innerhalb der Füllung (4:5-Format, 1080x1350)

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


def _row_to_reading(row):
    last = row["json"]
    return {
        "ts": row["ts"],
        "soil": last["soil"]["percent"],
        "soil_raw": last["soil"].get("raw"),
        "temp": last.get("climate", {}).get("temperature_c"),
        "hum": last.get("climate", {}).get("humidity_pct"),
        "light_visible": last.get("light", {}).get("visible"),
    }


def fetch_readings(n=1):
    """Die letzten n gültigen Messungen, älteste zuerst (die neueste steht am Ende)."""
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
    return [_row_to_reading(r) for r in rows[-n:]]


def fetch_latest_reading():
    return fetch_readings(1)[-1]


def reading_age_hours(reading, now=None):
    """Alter der Messung in Stunden. None, wenn der Zeitstempel nicht lesbar ist
    (dann wird die Aktualitätsprüfung übersprungen statt fälschlich zu blockieren)."""
    ts = reading.get("ts")
    now = now or datetime.now(LOCAL_TZ)
    try:
        if isinstance(ts, (int, float)):
            seconds = ts / 1000 if ts > 1e11 else ts
            when = datetime.fromtimestamp(seconds, LOCAL_TZ)
        elif isinstance(ts, str):
            if ts.replace(".", "", 1).isdigit():
                v = float(ts)
                when = datetime.fromtimestamp(v / 1000 if v > 1e11 else v, LOCAL_TZ)
            else:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=ZoneInfo("UTC"))
        else:
            return None
    except (ValueError, OverflowError, OSError):
        return None
    return (now - when).total_seconds() / 3600


def band_with_hysteresis(value, lo, hi, prev, margin):
    """Wie band(), aber mit Puffer: Wer schon 'low'/'high' ist, wird erst
    wieder 'ok', wenn der Wert um 'margin' klar im grünen Bereich liegt.
    Verhindert, dass ein Wert, der um die Grenze pendelt, ständig kippt."""
    if value is None:
        return "na"
    if prev == "low" and value < lo + margin:
        return "low"
    if prev == "high" and value > hi - margin:
        return "high"
    return band(value, lo, hi)


def evaluate(reading, prev_state=None):
    prev_state = prev_state or {}
    soil_state = band_with_hysteresis(reading["soil"], SOIL_LOW, SOIL_HIGH, prev_state.get("soil"), SOIL_MARGIN)
    temp_state = band_with_hysteresis(reading["temp"], TEMP_LOW, TEMP_HIGH, prev_state.get("temp"), TEMP_MARGIN)
    hum_state = band_with_hysteresis(reading["hum"], HUM_LOW, HUM_HIGH, prev_state.get("hum"), HUM_MARGIN)

    # Trockene Luft nur als Hinweis: Status bleibt "ok", der Hinweis wird separat gemerkt.
    hum_hint = hum_state == "low" and not HUM_LOW_IS_PROBLEM
    if hum_hint:
        hum_state = "ok"

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

    return {"soil": soil_state, "temp": temp_state, "hum": hum_state, "mood": mood, "hum_hint": hum_hint}


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


DAILY_STATUS_TEMPLATES = {
    "thirsty": [
        "Hallo, hier ist {name}. Noch immer durstig bei {soil:.0f}% Erde — ich warte weiter auf Wasser.",
        "{name} meldet sich: leider immer noch zu trocken ({soil:.0f}% Erde). Eine Gießkanne wäre super.",
        "Kurzes Hallo von {name} — die Erde liegt weiter bei {soil:.0f}%, das ist mir immer noch zu wenig.",
    ],
    "soggy": [
        "Hallo, hier ist {name}. Es ist immer noch ziemlich nass bei mir ({soil:.0f}% Erde) — ich brauch weiter eine Gießpause.",
        "{name} meldet sich: die Erde ist mit {soil:.0f}% weiter zu feucht. Bitte noch nicht wieder gießen.",
        "Hallo von {name} — ich stehe immer noch etwas im Nassen ({soil:.0f}% Erde). Geduld, bitte.",
    ],
    "cold": [
        "Hallo, hier ist {name}. Bei {temp:.1f}°C ist es mir weiterhin nicht angenehm — noch nicht besser geworden.",
        "{name} meldet sich: die Temperatur liegt weiter bei {temp:.1f}°C, das passt mir noch nicht.",
        "Kurzes Hallo von {name} — {temp:.1f}°C sind für mich immer noch nicht ideal.",
    ],
    "air": [
        "Hallo, hier ist {name}. Die Luft ist weiterhin trocken ({hum:.0f}%) — ein Luftbefeuchter wäre immer noch super.",
        "{name} meldet sich: {hum:.0f}% Luftfeuchte, das ist mir noch immer zu trocken.",
        "Hallo von {name} — meine Blätter warten weiter auf mehr Feuchtigkeit ({hum:.0f}% Luft).",
    ],
}


# Wie DAILY_STATUS_TEMPLATES["thirsty"], nur mit Tageszähler ({days}). Wird
# benutzt, sobald Knospi mindestens den 2. Tag in Folge zu trocken ist.
DAILY_THIRSTY_DAYS_TEMPLATES = [
    "Hallo, hier ist {name}. Das ist jetzt der {days}. Tag in Folge ohne Wasser — die Erde liegt bei {soil:.0f}%.",
    "{name} meldet sich: {days} Tage in Folge kein Wasser, die Erde liegt bei {soil:.0f}%. Langsam wird's echt trocken.",
    "Kurzes Hallo von {name} — Tag {days} ohne Gießen ({soil:.0f}% Erde). Ich bin noch tapfer, aber nicht mehr lange.",
]


def thirsty_days(prev_state, states, now=None):
    """Der wievielte Kalendertag in Folge Knospi zu trocken ist (1 = der Tag, an
    dem sie zu trocken wurde). Gezählt ab dem letzten Erdfeuchte-Post (last_event),
    also ab dem Moment, in dem der Bot sie als 'zu trocken' gemeldet hat. Wird sie
    wieder 'ok' (Gießen), beginnt die Zählung beim nächsten Mal von vorn.
    Gibt None zurück, wenn sie nicht zu trocken ist oder der Startzeitpunkt fehlt."""
    if states.get("soil") != "low" or prev_state.get("soil") != "low":
        return None
    since_iso = (prev_state.get("last_event") or {}).get("soil")
    if not since_iso:
        return None
    try:
        since = datetime.fromisoformat(since_iso)
    except (ValueError, TypeError):
        return None
    now = now or datetime.now(LOCAL_TZ)
    return (now.astimezone(LOCAL_TZ).date() - since.astimezone(LOCAL_TZ).date()).days + 1


# Hallo-Sprüche für "sonst alles gut, nur die Luft ist etwas trocken" (nur ein
# Hinweis, kein Problem). Die normalen Hallo-Sprüche loben teils die Luftfeuchte
# und passen dann nicht.
HELLO_AIR_HINT_TEMPLATES = [
    "Hallo, hier ist {name}! Alles im grünen Bereich, nur die Luft ist mit {hum:.0f}% etwas trocken — halb so wild, ich komm klar.",
    "{name} meldet sich: Erde {soil:.0f}%, {temp:.1f}°C, mir geht's gut. Die Luft ist mit {hum:.0f}% eher trocken, aber damit kann ich leben.",
    "Guten Tag von {name}! Kleiner Hinweis am Rande: {hum:.0f}% Luftfeuchte ist nicht mein Traum, aber sonst ist alles prima.",
    "Psst, {name} hier. Erde passt ({soil:.0f}%), Temperatur auch. Nur die Luft ({hum:.0f}%) könnte etwas feuchter sein — kein Drama.",
]


def hello_message(reading, states, days_thirsty=None):
    mood = states.get("mood", "happy")
    if mood == "thirsty" and days_thirsty and days_thirsty >= 2:
        templates = DAILY_THIRSTY_DAYS_TEMPLATES
        return random.choice(templates).format(name=PLANT_NAME, days=days_thirsty, **reading)
    if mood == "happy" and states.get("hum_hint"):
        return random.choice(HELLO_AIR_HINT_TEMPLATES).format(name=PLANT_NAME, **reading)
    templates = DAILY_STATUS_TEMPLATES.get(mood)
    if not templates:
        # "happy" (oder unbekannte Stimmung) -> normale, unbeschwerte Hallo-Sprüche
        templates = HELLO_TEMPLATES
    return random.choice(templates).format(name=PLANT_NAME, **reading)


def tip_message(reading, key, new_state):
    templates = TIP_TEMPLATES.get(f"{key}_{new_state}")
    if not templates:
        return f"{key} kippt nach {new_state} ({reading.get(key)})."
    return random.choice(templates).format(**reading)


# Kurzer Nachsatz für "Danke"-Posts: Ist nur ein Wert wieder gut, andere aber
# noch nicht, soll Knospi das dazusagen. Sonst wirkt ein fröhlicher Text neben
# einem traurigen Gesicht (das Bild zeigt immer die Gesamtstimmung).
REMAINING_NOTES = {
    "soil_low": "Mit der Erde ({soil:.0f}%) bin ich aber noch zu durstig.",
    "soil_high": "Die Erde ist mit {soil:.0f}% aber noch zu nass.",
    "temp_low": "Bei {temp:.1f}°C friere ich aber noch.",
    "temp_high": "{temp:.1f}°C sind mir aber noch zu warm.",
    "hum_low": "Die Luft ({hum:.0f}%) ist aber noch zu trocken.",
    "hum_high": "Die Luft ({hum:.0f}%) ist aber noch zu feucht.",
}


def improve_message(reading, key, states=None):
    templates = IMPROVE_TEMPLATES.get(key, [])
    if not templates:
        text = f"{key} ist wieder okay."
    else:
        text = random.choice(templates).format(**reading)

    # Welche anderen Werte sind noch nicht in Ordnung?
    notes = []
    for other in ("soil", "hum", "temp"):
        if other == key or not states:
            continue
        note = REMAINING_NOTES.get(f"{other}_{states.get(other)}")
        if note:
            notes.append(note.format(**reading))
    if notes:
        text = f"{text} {' '.join(notes)}"
    return text


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

def _default_state():
    # last_event: wann zuletzt ein Post zu diesem Wert raus ging.
    # post_log:   Protokoll aller Posts (für Tages-/Wochenlimits und Ruhezeiten).
    # pool:       Verlauf der Pool-Posts (Wiederholungen, Säulen-Verteilung, CTA-Takt).
    return {
        "soil": "ok", "temp": "ok", "hum": "ok",
        "last_daily": None, "last_event": {},
        "post_log": [], "pool": {"history": [], "since_cta": 0},
    }


def load_state():
    state = _default_state()
    if STATE_PATH.exists():
        try:
            loaded = json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError as e:
            # Kaputtes/handbearbeitetes state.json soll den Lauf nicht crashen -
            # stattdessen mit sicheren Standardwerten weitermachen und warnen.
            print(f"WARNUNG: state.json ist kein gültiges JSON ({e}). Nutze Standardwerte.")
            return state
        if isinstance(loaded, dict):
            state.update(loaded)  # ältere state.json ohne neue Felder funktionieren weiter
        if not isinstance(state.get("last_event"), dict):
            state["last_event"] = {}
        if not isinstance(state.get("pool"), dict):
            state["pool"] = {"history": [], "since_cta": 0}
        state["pool"].setdefault("history", [])
        state["pool"].setdefault("since_cta", 0)
        if not isinstance(state.get("post_log"), list):
            state["post_log"] = []
        # Altes state.json ohne Protokoll: aus last_daily/last_event ein Start-Protokoll bauen,
        # damit das Tageslimit schon am ersten Tag nach dem Umstieg stimmt.
        if not state["post_log"]:
            if state.get("last_daily"):
                state["post_log"].append({"ts": state["last_daily"], "kind": "daily", "key": None})
            for key, ts in state["last_event"].items():
                state["post_log"].append({"ts": ts, "kind": "legacy", "key": key})
    return state


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _tip_in_cooldown(prev_state, key, now):
    """True, wenn zu diesem Wert vor weniger als TIP_COOLDOWN_HOURS zuletzt gepostet wurde."""
    last_iso = (prev_state.get("last_event") or {}).get(key)
    if not last_iso:
        return False
    try:
        last = datetime.fromisoformat(last_iso)
    except (ValueError, TypeError):
        return False
    return now - last < timedelta(hours=TIP_COOLDOWN_HOURS)


def decide_events(prev_state, new_states, now=None):
    """Liefert (events, suppressed).
    events: Liste von ('tip'|'improve', key, from_state, to_state).
    suppressed: Werte, bei denen ein 'wird schlecht'-Post wegen der Pause
    (TIP_COOLDOWN_HOURS) noch zurückgehalten wird. Der gespeicherte Zustand
    bleibt dort unverändert, sodass der Post später nachgeholt wird, falls der
    Wert dann immer noch schlecht ist."""
    now = now or datetime.now(LOCAL_TZ)
    events = []
    suppressed = []
    for key in ("soil", "temp", "hum"):
        old, new = prev_state.get(key, "ok"), new_states[key]
        if old == new or new == "na":
            continue
        if new in ("low", "high"):
            # auch z.B. von zu trocken auf zu nass gesprungen -> zählt als "kippt"
            if _tip_in_cooldown(prev_state, key, now):
                suppressed.append(key)
            else:
                events.append(("tip", key, old, new))
        elif new == "ok" and old in ("low", "high"):
            events.append(("improve", key, old, new))  # Danke immer sofort
    return events, suppressed


def is_daily_due(last_daily_iso, now=None):
    """True, wenn heute noch kein Hallo-Post raus ist UND die aktuelle lokale
    Uhrzeit innerhalb von DAILY_WINDOW_START/-END liegt."""
    now_local = now or datetime.now(LOCAL_TZ)

    if not (DAILY_WINDOW_START <= now_local.time() <= DAILY_WINDOW_END):
        return False

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
# Regelwerk: wann darf gepostet werden? (Ruhezeit, Tages-/Wochenlimits, Stabilität)
# ---------------------------------------------------------------------------

def in_quiet_hours(now):
    t = now.time()
    return t >= QUIET_START or t < QUIET_END


def _parse_iso(value):
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt


def _log_entries(state, kinds=None, key=None, st=None, since=None):
    out = []
    for entry in state.get("post_log") or []:
        ts = _parse_iso(entry.get("ts"))
        if ts is None:
            continue
        if since is not None and ts < since:
            continue
        if kinds and entry.get("kind") not in kinds:
            continue
        if key is not None and entry.get("key") != key:
            continue
        if st is not None and entry.get("state") != st:
            continue
        out.append(entry)
    return out


def _is_today(entry, now):
    ts = _parse_iso(entry.get("ts"))
    return ts is not None and ts.astimezone(LOCAL_TZ).date() == now.date()


def automated_posts_today(state, now):
    """Wie viele automatische Sensor-Posts (alles außer Pool-Posts) heute schon raus sind."""
    return [e for e in state.get("post_log") or [] if e.get("kind") != "pool" and _is_today(e, now)]


def pool_posts_today(state, now):
    return [e for e in state.get("post_log") or [] if e.get("kind") == "pool" and _is_today(e, now)]


def automated_post_allowed(state, now):
    """(erlaubt?, Grund) für einen automatischen Sensor-Post."""
    if in_quiet_hours(now):
        return False, f"Ruhezeit ({QUIET_START:%H:%M}-{QUIET_END:%H:%M} Uhr)"
    if len(automated_posts_today(state, now)) >= MAX_AUTOMATED_POSTS_PER_DAY:
        return False, "heute ist schon ein automatischer Post raus"
    return True, ""


def tip_allowed(state, key, new, reading, now):
    """(erlaubt?, Grund) für einen 'wird schlecht'-Post: Wochenlimit und Mindestabstand pro Zustand.
    Bei außergewöhnlichen Werten entfallen die Wochen-/Monatslimits."""
    name = f"{key}_{new}"
    check = EXTRAORDINARY.get(name)
    value = reading.get(key)
    if check and value is not None and check(value):
        return True, ""
    week = _log_entries(state, kinds={"tip"}, since=now - timedelta(days=7))
    if len(week) >= MAX_TIPS_PER_WEEK:
        return False, f"Wochenlimit ({MAX_TIPS_PER_WEEK} 'wird schlecht'-Posts) erreicht"
    days = TIP_MIN_DAYS_BETWEEN.get(name, 7)
    recent = _log_entries(state, kinds={"tip"}, key=key, st=new, since=now - timedelta(days=days))
    if recent:
        return False, f"zu '{name}' kam in den letzten {days} Tagen schon ein Post"
    return True, ""


def confirmed_states(readings, prev_state):
    """Zustand je Wert, der in ALLEN übergebenen Messungen gleich ist (sonst None).
    So löst ein einzelner Ausreißer keinen Post aus."""
    per_reading = [evaluate(r, prev_state) for r in readings]
    out = {}
    for key in ("soil", "temp", "hum"):
        values = {p[key] for p in per_reading}
        out[key] = values.pop() if len(values) == 1 else None
    return out


def primary_problem(states):
    """Das wichtigste aktuelle Problem als (wert, zustand) oder None, wenn alles in Ordnung ist."""
    if states["soil"] == "low":
        return "soil", "low"
    if HUM_LOW_IS_PROBLEM and states["hum"] == "low":
        return "hum", "low"
    if states["temp"] in ("low", "high"):
        return "temp", states["temp"]
    if states["soil"] == "high":
        return "soil", "high"
    if states["hum"] == "high":
        return "hum", "high"
    return None


def record_post(state, now, kind, key=None, st=None, extra=None):
    """Neuer Zustand mit einem zusätzlichen Eintrag im Post-Protokoll (altes wird aufgeräumt)."""
    new_state = dict(state)
    cutoff = now - timedelta(days=POST_LOG_KEEP_DAYS)
    log = [e for e in (state.get("post_log") or []) if (_parse_iso(e.get("ts")) or now) >= cutoff]
    entry = {"ts": now.isoformat(), "kind": kind, "key": key}
    if st is not None:
        entry["state"] = st
    if extra:
        entry.update(extra)
    log.append(entry)
    new_state["post_log"] = log
    return new_state


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
# Bilder für Pool-Posts: Knospi-Kachel mit eigenem Bildtext und Format-Label,
# bzw. Pflanzen-WG-Szene aus den festen Figuren-Assets (assets/characters/...)
# ---------------------------------------------------------------------------

CREAM = (255, 253, 247)
LIME = (223, 244, 143)
PILL_DOT = (160, 222, 100)
GRAY_TEXT = (96, 108, 88)


def _pill_geometry(draw, label, right=1016, top=1203, bottom=1290):
    """Schrift und Position der dunklen Label-Pille unten rechts (wie in den Vorlagen)."""
    size = 44
    font = load_font(size)
    while draw.textbbox((0, 0), label, font=font)[2] > 430 and size > 28:
        size -= 2
        font = load_font(size)
    tb = draw.textbbox((0, 0), label, font=font)
    pad, dot, gap = 38, 24, 14
    width = pad + dot + gap + (tb[2] - tb[0]) + pad
    return {"font": font, "tb": tb, "pad": pad, "dot": dot, "gap": gap,
            "left": right - width, "right": right, "top": top, "bottom": bottom}


def _draw_pill(draw, label, right=1016, top=1203, bottom=1290):
    g = _pill_geometry(draw, label, right, top, bottom)
    left, tb = g["left"], g["tb"]
    draw.rounded_rectangle((left, top, right, bottom), radius=(bottom - top) // 2, fill=INK)
    cy = (top + bottom) // 2
    draw.ellipse((left + g["pad"], cy - g["dot"] // 2, left + g["pad"] + g["dot"], cy + g["dot"] // 2), fill=PILL_DOT)
    tx = left + g["pad"] + g["dot"] + g["gap"] - tb[0]
    ty = cy - (tb[1] + tb[3]) / 2
    draw.text((tx, ty), label, font=g["font"], fill=CREAM)
    return left


def _draw_centered_lines(draw, lines, font, line_h, box):
    x0, y0, x1, y1 = box
    y = y0 + ((y1 - y0) - line_h * len(lines)) // 2
    cx = (x0 + x1) // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        draw.text((cx - (bbox[2] - bbox[0]) // 2, y), line, font=font, fill=INK)
        y += line_h


def render_pool_post(entry, out_path):
    """Knospi-Kachel (Ausdruck laut Eintrag) mit dem Bildtext in der Sprechblase.
    Die baked-in Stimmungs-Pille ('Rundum wohl' o.ä.) wird durch das Format ersetzt
    ('Knospi klärt das' ...), damit der Post keine Sensor-Aussage vortäuscht."""
    img = Image.open(MOOD_TEMPLATE[entry["expression"]]).convert("RGB")
    draw = ImageDraw.Draw(img)
    font, lines, line_h = _fit_text(draw, entry["image_text"], BUBBLE_BOX)
    _draw_centered_lines(draw, lines, font, line_h, BUBBLE_BOX)
    draw.rectangle((620, 1165, img.width, img.height), fill=CREAM)
    _draw_pill(draw, FORMAT_LABEL[entry["format"]])
    img.save(out_path, "PNG")
    return out_path


def _draw_bubble(draw, box, tail_x):
    x0, y0, x1, y1 = box
    draw.rounded_rectangle((x0, y0 + 10, x1, y1 + 10), radius=40, fill=(205, 222, 150))
    draw.rounded_rectangle(box, radius=40, fill=CREAM, outline=INK, width=6)
    tx = max(x0 + 70, min(x1 - 70, tail_x))
    draw.polygon([(tx - 24, y1 - 3), (tx + 24, y1 - 3), (tx, y1 + 34)], fill=CREAM)
    draw.line([(tx - 24, y1), (tx, y1 + 34), (tx + 24, y1)], fill=INK, width=6, joint="curve")
    draw.line([(tx - 21, y1), (tx + 21, y1)], fill=CREAM, width=8)


def render_scene(entry, out_path):
    """Pflanzen-WG-Szene: feste Figuren-Assets unten, Sprechblasen oben (1080x1350)."""
    scene = entry["scene"]
    W, H = 1080, 1350
    glow = Image.new("RGB", (W, H), LIME)
    gd = ImageDraw.Draw(glow)
    gd.ellipse((-250, -300, 800, 420), fill=(238, 250, 190))
    gd.ellipse((620, 480, 1350, 980), fill=(236, 249, 186))
    img = glow.filter(ImageFilter.GaussianBlur(120))
    draw = ImageDraw.Draw(img)

    # Figuren in einer Reihe, auf einer gemeinsamen Standlinie
    cast = scene["cast"]
    n = len(cast)
    slot_w = (W - 80) / n
    ground_y = 1115
    sprites = []
    for member in cast:
        sprite = Image.open(char_path(member["char"], member["expression"])).convert("RGBA")
        box = sprite.getchannel("A").getbbox()  # transparenten Rand abschneiden
        sprites.append(sprite.crop(box) if box else sprite)
    # Ein gemeinsamer Maßstab für alle Figuren, damit die Größenverhältnisse stimmen
    # (Pepes flacher Topf bleibt kleiner als Monis Topf)
    scale = min(min((slot_w - 24) / sp.width, 560 / sp.height) for sp in sprites)
    centers = {}
    for i, (member, char) in enumerate(zip(cast, sprites)):
        char = char.resize((max(1, int(char.width * scale)), max(1, int(char.height * scale))), Image.LANCZOS)
        cx = int(40 + slot_w * (i + 0.5))
        img.paste(char, (cx - char.width // 2, ground_y - char.height), char)
        half = int(min(char.width / 2 + 24, slot_w / 2 - 6))
        draw.line([(cx - half, ground_y + 10), (cx + half, ground_y + 10)], fill=INK, width=8)
        centers[member["char"]] = cx

    # Sprechblasen von oben nach unten in Sprechreihenfolge
    bubble_w = 760
    y = 70
    for line in scene["lines"]:
        font, lines, line_h = _fit_text(draw, line["text"], (0, 0, bubble_w - 90, 150), start_size=46, min_size=28)
        h = line_h * len(lines) + 56
        cx = centers[line["char"]]
        x0 = max(40, min(W - 40 - bubble_w, cx - bubble_w // 2))
        box = (x0, y, x0 + bubble_w, y + h)
        _draw_bubble(draw, box, cx)
        _draw_centered_lines(draw, lines, font, line_h, box)
        y += h + 46

    # Fußleiste wie in den Kacheln: Titel links, Format-Label rechts
    draw.rectangle((0, 1150, W, H), fill=CREAM)
    draw.rectangle((0, 1150, W, 1156), fill=INK)
    pill_left = _pill_geometry(draw, FORMAT_LABEL[entry["format"]])["left"]
    size = 62
    title = "Die Pflanzen-WG"
    font = load_font(size)
    while draw.textbbox((0, 0), title, font=font)[2] > pill_left - 66 - 30 and size > 30:
        size -= 2
        font = load_font(size)
    draw.text((66, 1190), title, font=font, fill=INK)
    names = " · ".join(m["char"].capitalize() for m in cast)
    sub_font = load_font(32, bold=False)
    while draw.textbbox((0, 0), names, font=sub_font)[2] > pill_left - 66 - 30 and sub_font.size > 20:
        sub_font = load_font(sub_font.size - 2, bold=False)
    draw.text((66, 1262), names, font=sub_font, fill=GRAY_TEXT)
    _draw_pill(draw, FORMAT_LABEL[entry["format"]])
    img.save(out_path, "PNG")
    return out_path


def render_pool_image(entry, out_path):
    out_path.parent.mkdir(exist_ok=True)
    if entry.get("scene"):
        return render_scene(entry, out_path)
    return render_pool_post(entry, out_path)


# ---------------------------------------------------------------------------
# GitHub: Bild committen, damit es unter einer öffentlichen raw.githubusercontent-URL
# erreichbar ist (die Instagram Graph API braucht eine öffentliche Bild-URL, keinen Upload)
# ---------------------------------------------------------------------------

def _git_configured():
    subprocess.run(["git", "config", "user.name", "knospi-bot"], check=True)
    subprocess.run(["git", "config", "user.email", "knospi-bot@users.noreply.github.com"], check=True)


def commit_paths(paths, message):
    """Committet und pusht genau die übergebenen Pfade. Wird für das Bild und
    (separat, erst NACH einem bestätigten Instagram-Post) für state.json genutzt,
    damit ein fehlgeschlagener Post niemals als 'erledigt' im Git-Verlauf landet."""
    _git_configured()
    subprocess.run(["git", "add", *[str(p) for p in paths]], check=True)
    # Nichts zu committen (z.B. state.json unverändert) ist kein Fehler.
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        return
    subprocess.run(["git", "commit", "-m", message], check=True)
    push = subprocess.run(["git", "push"])
    if push.returncode != 0:
        # Zwischenzeitlich wurde im Repo etwas geändert (z.B. von dir im Browser):
        # erst die neuen Änderungen holen, dann nochmal pushen.
        subprocess.run(["git", "pull", "--rebase"], check=True)
        subprocess.run(["git", "push"], check=True)


def commit_and_get_public_url(image_path: Path):
    """Committet NUR das Bild (nicht state.json!) und liefert die öffentliche
    raw.githubusercontent-URL, unter der Instagram es abrufen kann."""
    repo = os.environ["GITHUB_REPOSITORY"]  # z.B. "meinuser/knospi-bot"
    branch = os.environ.get("GITHUB_REF_NAME", "main")

    commit_paths([image_path], f"post: {image_path.name}")

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

    return _publish_with_retry(ig_user_id, creation_id, access_token)


def _publish_with_retry(ig_user_id, creation_id, access_token, max_attempts=6, delay_seconds=10):
    """Veröffentlicht den Media-Container. Obwohl _wait_for_container_ready
    schon FINISHED gemeldet hat, lehnt Instagram media_publish manchmal
    trotzdem kurzzeitig mit 'Media ID is not available' (code 9007,
    error_subcode 2207027) ab - ein bekanntes, dokumentiertes Race-Condition-
    Verhalten auf Meta-Seite (das Medium ist laut Status fertig, intern aber
    noch nicht ganz publizierbereit). Genau diesen einen Fehler behandeln wir
    daher als vorübergehend und versuchen es nach kurzer Pause erneut; jeder
    andere Fehler schlägt sofort fehl."""
    last_resp = None
    for attempt in range(1, max_attempts + 1):
        publish_resp = requests.post(
            f"https://graph.facebook.com/{GRAPH_VERSION}/{ig_user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": access_token},
            timeout=30,
        )
        if publish_resp.ok:
            return publish_resp.json()

        last_resp = publish_resp
        try:
            error = publish_resp.json().get("error", {})
        except ValueError:
            error = {}

        is_transient = error.get("code") == 9007 and error.get("error_subcode") == 2207027
        if is_transient and attempt < max_attempts:
            print(
                f"Medien noch nicht publizierbereit (Versuch {attempt}/{max_attempts}), "
                f"warte {delay_seconds}s und versuche es erneut: {error}"
            )
            time.sleep(delay_seconds)
            continue

        print("Fehlerantwort beim Veröffentlichen:", publish_resp.text)
        publish_resp.raise_for_status()

    last_resp.raise_for_status()  # pragma: no cover - nur falls Schleife unerwartet ohne return endet


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
# Inhalts-Pool: vorbereitete Posts (content_pool.json), Auswahl, Entwürfe, Freigabe
# ---------------------------------------------------------------------------

EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐⭕]")


def char_path(char, expression):
    """Pfad zur Figuren-Datei oder (wenn keine existiert) der Standardpfad."""
    for pattern in CHAR_SEARCH_DIRS:
        folder = _ROOT / pattern.format(c=char)
        for name in (f"{char}-{expression}.png", f"{expression}.png"):
            if (folder / name).exists():
                return folder / name
    return CHAR_DIR / char / f"{char}-{expression}.png"


def season_of(now):
    return {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer"}.get(now.month, "autumn")


def _words(text):
    return len(str(text).split())


def _validate_scene(scene):
    if not isinstance(scene, dict):
        return ["scene ist kein Objekt"]
    errors = []
    cast = scene.get("cast")
    chars = []
    if not isinstance(cast, list) or not 2 <= len(cast) <= 5:
        errors.append("scene.cast: 2 bis 5 Figuren")
    else:
        for member in cast:
            if not isinstance(member, dict) or member.get("char") not in CHARACTERS \
                    or member.get("expression") not in EXPRESSIONS:
                errors.append(f"scene.cast: ungültige Figur {member!r}")
            else:
                chars.append(member["char"])
        if len(set(chars)) != len(chars):
            errors.append("scene.cast: Figur kommt doppelt vor")
    lines = scene.get("lines")
    if not isinstance(lines, list) or not 1 <= len(lines) <= 3:
        errors.append("scene.lines: 1 bis 3 Sprechblasen")
    else:
        speakers = []
        for line in lines:
            if not isinstance(line, dict) or line.get("char") not in chars \
                    or not isinstance(line.get("text"), str) or not line["text"].strip():
                errors.append(f"scene.lines: ungültige Zeile {line!r}")
                continue
            speakers.append(line["char"])
            if _words(line["text"]) > 7:
                errors.append(f"scene.lines: mehr als 7 Wörter: {line['text']!r}")
        if len(set(speakers)) != len(speakers):
            errors.append("scene.lines: jede Figur hat höchstens eine Sprechblase")
    return errors


def validate_entry(entry):
    """Prüft einen Pool-Eintrag gegen die Regeln des Briefings. Leere Liste = in Ordnung."""
    if not isinstance(entry, dict):
        return ["Eintrag ist kein Objekt"]
    errors = []
    eid = entry.get("id")
    if not isinstance(eid, str) or not re.fullmatch(r"[a-z0-9_\-]+", eid):
        errors.append("id fehlt oder ungültig (nur a-z, 0-9, _ und -)")
    if entry.get("pillar") not in PILLAR_TARGETS:
        errors.append(f"pillar muss eines von {sorted(PILLAR_TARGETS)} sein")
    if entry.get("format") not in FORMAT_LABEL:
        errors.append(f"format muss eines von {sorted(FORMAT_LABEL)} sein")
    caption = entry.get("caption")
    if not isinstance(caption, str) or not caption.strip():
        errors.append("caption fehlt")
        caption = ""
    else:
        sentences = len(re.findall(r"[.!?…]+(?=\s|$)", caption)) or 1
        if sentences > 3:
            errors.append(f"caption hat {sentences} Sätze (höchstens 3)")
        if len(caption) > 300:
            errors.append("caption ist zu lang (über 300 Zeichen)")
    cta = entry.get("cta")
    if cta is not None and (not isinstance(cta, str) or not cta.strip()):
        errors.append("cta ist leer")
        cta = ""
    if len(EMOJI_RE.findall(caption + " " + (cta or ""))) > 1:
        errors.append("mehr als 1 Emoji in Caption + CTA")
    tags = entry.get("hashtags")
    if not isinstance(tags, list) or not 1 <= len(tags) <= 3 \
            or not all(isinstance(t, str) and re.fullmatch(r"#\w+", t) for t in tags):
        errors.append("hashtags: 1 bis 3 zusätzliche Hashtags mit # (#knospi #pflanzenliebe kommen automatisch dazu)")
    seasons = entry.get("seasons")
    if seasons is not None and (not isinstance(seasons, list) or not set(seasons) <= SEASONS):
        errors.append(f"seasons darf nur {sorted(SEASONS)} enthalten")
    if entry.get("scene") is None:
        text = entry.get("image_text")
        if not isinstance(text, str) or not text.strip():
            errors.append("image_text fehlt")
        elif _words(text) > 7:
            errors.append(f"image_text hat {_words(text)} Wörter (höchstens 7)")
        if entry.get("expression") not in MOOD_TEMPLATE:
            errors.append(f"expression muss eines von {sorted(MOOD_TEMPLATE)} sein")
    else:
        errors += _validate_scene(entry["scene"])
    return errors


def missing_assets(entry):
    """Dateien, die für dieses Bild fehlen (Figuren-Assets bzw. Knospi-Kachel)."""
    if entry.get("scene"):
        needed = [char_path(m["char"], m["expression"]) for m in entry["scene"]["cast"]]
    else:
        needed = [MOOD_TEMPLATE[entry["expression"]]]
    return [p for p in needed if not p.exists()]


def load_pool():
    if not POOL_PATH.exists():
        return []
    try:
        data = json.loads(POOL_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"WARNUNG: content_pool.json ist kein gültiges JSON ({e}). Pool wird übersprungen.")
        return []
    entries = data.get("entries", []) if isinstance(data, dict) else data
    return entries if isinstance(entries, list) else []


def usable_pool(pool):
    """Gültige Einträge. Ungültige werden mit Begründung gemeldet und übersprungen."""
    good = []
    for entry in pool:
        errors = validate_entry(entry)
        if errors:
            name = entry.get("id", "?") if isinstance(entry, dict) else "?"
            print(f"WARNUNG: Pool-Eintrag '{name}' übersprungen: {'; '.join(errors)}")
        else:
            good.append(entry)
    return good


def choose_pool_entry(pool, state, now, rng=random):
    """Wählt einen Pool-Eintrag mit redaktioneller Begründung.
    Berücksichtigt: Jahreszeit, Wiederholungen (120 Tage), letztes Format, Verteilung der
    Inhalts-Säulen, CTA-Takt und ob alle Bilder/Figuren vorhanden sind.
    Gibt (eintrag, begründung, cta_verwenden) zurück oder (None, grund, False)."""
    history = (state.get("pool") or {}).get("history", [])
    cutoff = now - timedelta(days=POOL_REPEAT_DAYS)
    recently_used = {h.get("id") for h in history if (_parse_iso(h.get("ts")) or now) >= cutoff}
    season = season_of(now)

    candidates = []
    skipped_assets = 0
    for entry in pool:
        if entry["id"] in recently_used:
            continue
        if entry.get("seasons") and season not in entry["seasons"]:
            continue
        if missing_assets(entry):
            skipped_assets += 1
            continue
        candidates.append(entry)
    if not candidates:
        hint = f" ({skipped_assets} Einträge warten auf fehlende Figuren-Assets)" if skipped_assets else ""
        return None, "kein passender, noch nicht verwendeter Inhalt verfügbar" + hint, False

    last_format = history[-1].get("format") if history else None
    different = [e for e in candidates if e["format"] != last_format]
    if different:
        candidates = different

    recent = history[-12:]
    total = len(recent)
    counts = {p: sum(1 for h in recent if h.get("pillar") == p) for p in PILLAR_TARGETS}
    since_cta = (state.get("pool") or {}).get("since_cta", 0)
    cta_due = since_cta >= CTA_EVERY - 1

    weights = []
    for entry in candidates:
        pillar = entry["pillar"]
        actual = counts[pillar] / total if total else PILLAR_TARGETS[pillar]
        weight = max(0.1, PILLAR_TARGETS[pillar] - actual + 0.25)
        if entry.get("seasons"):
            weight *= 1.5
        if cta_due and entry.get("cta"):
            weight *= 2
        weights.append(weight)
    entry = rng.choices(candidates, weights=weights, k=1)[0]

    use_cta = bool(cta_due and entry.get("cta"))
    actual = counts[entry["pillar"]] / total if total else 0
    weekday = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"][now.weekday()]
    reason = (
        f"{weekday}-Slot · Säule {PILLAR_LABEL[entry['pillar']]} "
        f"(zuletzt {actual:.0%}, Wunsch {PILLAR_TARGETS[entry['pillar']]:.0%}) · "
        f"Format {FORMAT_LABEL[entry['format']]}"
    )
    if entry.get("seasons"):
        reason += " · passt zur Jahreszeit"
    if last_format and entry["format"] != last_format:
        reason += " · anderes Format als der Post davor"
    reason += " · in den letzten 120 Tagen nicht verwendet"
    if use_cta:
        reason += " · mit sanftem Aufruf (jeder dritte Post)"
    return entry, reason, use_cta


def compose_caption(entry, use_cta):
    parts = [entry["caption"].strip()]
    if use_cta and entry.get("cta"):
        parts.append(entry["cta"].strip())
    tags = []
    for tag in ["#knospi", "#pflanzenliebe"] + list(entry["hashtags"]):
        if tag.lower() not in [t.lower() for t in tags]:
            tags.append(tag)
    return "\n\n".join(parts) + "\n\n" + " ".join(tags)


def entry_image_text(entry):
    if entry.get("scene"):
        return " / ".join(f"{l['char'].capitalize()}: {l['text']}" for l in entry["scene"]["lines"])
    return entry["image_text"]


# --- Entwürfe & GitHub-Issues -------------------------------------------------

def draft_json_path(draft_id):
    return DRAFTS_DIR / f"{draft_id}.json"


def load_drafts():
    drafts = []
    if DRAFTS_DIR.exists():
        for path in sorted(DRAFTS_DIR.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                print(f"WARNUNG: {path.name} ist kein gültiges JSON und wird ignoriert.")
                continue
            if isinstance(data, dict) and data.get("id"):
                drafts.append(data)
    return drafts


def save_draft(draft):
    DRAFTS_DIR.mkdir(exist_ok=True)
    draft_json_path(draft["id"]).write_text(json.dumps(draft, indent=2, ensure_ascii=False), encoding="utf-8")


def _gh_headers():
    return {
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_create_issue(title, body, assignee=None):
    """Legt ein GitHub-Issue an (löst die E-Mail/Benachrichtigung an dich aus). None bei Fehler."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo or not os.environ.get("GITHUB_TOKEN"):
        print("HINWEIS: Kein GITHUB_REPOSITORY/GITHUB_TOKEN – Issue wird nicht angelegt.")
        return None
    payload = {"title": title, "body": body}
    if assignee:
        payload["assignees"] = [assignee]
    try:
        resp = requests.post(f"https://api.github.com/repos/{repo}/issues", json=payload,
                             headers=_gh_headers(), timeout=30)
        if resp.status_code == 422 and assignee:  # Zuweisung nicht möglich -> ohne versuchen
            payload.pop("assignees")
            resp = requests.post(f"https://api.github.com/repos/{repo}/issues", json=payload,
                                 headers=_gh_headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()["number"]
    except Exception as e:  # ein Problem mit dem Issue darf keinen Lauf abbrechen
        print(f"WARNUNG: Issue konnte nicht angelegt werden: {e}")
        return None


def gh_comment(number, text):
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo or not number or not os.environ.get("GITHUB_TOKEN"):
        return
    try:
        requests.post(f"https://api.github.com/repos/{repo}/issues/{number}/comments",
                      json={"body": text}, headers=_gh_headers(), timeout=30).raise_for_status()
    except Exception as e:
        print(f"WARNUNG: Kommentar im Issue #{number} fehlgeschlagen: {e}")


def gh_close_issue(number):
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo or not number or not os.environ.get("GITHUB_TOKEN"):
        return
    try:
        requests.patch(f"https://api.github.com/repos/{repo}/issues/{number}",
                       json={"state": "closed"}, headers=_gh_headers(), timeout=30).raise_for_status()
    except Exception as e:
        print(f"WARNUNG: Issue #{number} konnte nicht geschlossen werden: {e}")


def public_raw_url(path):
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    rel = Path(path).resolve().relative_to(Path(__file__).parent.resolve()) if Path(path).is_absolute() else Path(path)
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{rel.as_posix()}"


def draft_issue_body(draft, image_url):
    owner = os.environ.get("GITHUB_REPOSITORY", "/").split("/")[0]
    caption_quote = "\n".join("> " + line if line else ">" for line in draft["caption"].split("\n"))
    return (
        f"@{owner} Knospi hat einen neuen Entwurf vorbereitet.\n\n"
        f"![Vorschau]({image_url})\n\n"
        f"**Format:** {FORMAT_LABEL.get(draft['format'], draft['format'])} · "
        f"**Säule:** {PILLAR_LABEL.get(draft['pillar'], draft['pillar'])}\n\n"
        f"**Bildtext:** {draft['image_text']}\n\n"
        f"**Caption:**\n{caption_quote}\n\n"
        f"**Warum dieser Post:** {draft['reason']}\n\n"
        f"---\n"
        f"**Freigeben:** schreib unten einen Kommentar mit `ok`  \n"
        f"**Verwerfen:** schreib `nein`\n\n"
        f"Nach der Freigabe postet Knospi bei der nächsten Gelegenheit "
        f"(nicht zwischen {QUIET_START:%H}:{QUIET_START:%M} und {QUIET_END:%H}:{QUIET_END:%M} Uhr). "
        f"Ohne Antwort verfällt der Entwurf nach {DRAFT_REVIEW_DAYS} Tagen.\n\n"
        f"<!-- knospi-draft:{draft['id']} -->"
    )


def pool_draft_due(state, now):
    """Ist heute ein Pool-Slot offen? (Slot-Wochentag, Uhrzeit, Wochenlimit, kein offener Entwurf)"""
    if now.weekday() not in POOL_WEEKDAYS:
        return False
    if not (POOL_DRAFT_START <= now.time() <= POOL_DRAFT_END):
        return False
    history = state["pool"]["history"]
    this_week = [h for h in history
                 if (ts := _parse_iso(h.get("ts"))) and ts.astimezone(LOCAL_TZ).isocalendar()[:2] == now.isocalendar()[:2]]
    if len(this_week) >= POOL_MAX_PER_WEEK:
        return False
    if any(_is_today(h, now) for h in history):
        return False
    if any(d.get("status") in ("draft", "approved") for d in load_drafts()):
        return False
    return True


def create_pool_draft(state, now, dry_run=False, rng=random):
    """Wählt einen Pool-Inhalt, rendert das Bild, legt Entwurf + GitHub-Issue an. Gibt den neuen Zustand zurück."""
    pool = usable_pool(load_pool())
    entry, reason, use_cta = choose_pool_entry(pool, state, now, rng)
    if entry is None:
        print(f"Pool: {reason}.")
        return state

    draft_id = f"{now:%Y%m%d}-{entry['id']}"
    image_path = DRAFTS_DIR / f"{draft_id}.png"
    render_pool_image(entry, image_path)
    draft = {
        "id": draft_id, "entry_id": entry["id"], "status": "draft",
        "created_at": now.isoformat(), "format": entry["format"], "pillar": entry["pillar"],
        "image": f"drafts/{draft_id}.png", "image_text": entry_image_text(entry),
        "caption": compose_caption(entry, use_cta), "reason": reason, "used_cta": use_cta,
        "issue_number": None, "published_at": None,
    }
    print(f"Entwurf '{draft_id}' gerendert: {image_path}")
    print(f"  Begründung: {reason}")
    print(f"  Bildtext:   {draft['image_text']}")
    print(f"  Caption:\n{draft['caption']}")
    if dry_run:
        print("(--dry-run) Kein Entwurf gespeichert, kein Issue, kein Commit.")
        return state

    new_state = dict(state)
    pool_state = dict(state["pool"])
    pool_state["history"] = (state["pool"]["history"] + [{
        "id": entry["id"], "pillar": entry["pillar"], "format": entry["format"],
        "ts": now.isoformat(), "cta": use_cta,
    }])[-60:]
    pool_state["since_cta"] = 0 if use_cta else state["pool"].get("since_cta", 0) + 1
    new_state["pool"] = pool_state

    save_draft(draft)
    save_state(new_state)
    commit_paths([image_path, draft_json_path(draft_id), STATE_PATH], f"draft: {draft_id}")

    owner = os.environ.get("GITHUB_REPOSITORY", "/").split("/")[0]
    number = gh_create_issue(f"Entwurf: {draft['image_text'][:60]}",
                             draft_issue_body(draft, public_raw_url(image_path)), assignee=owner)
    if number:
        draft["issue_number"] = number
        save_draft(draft)
        commit_paths([draft_json_path(draft_id)], f"draft: Issue #{number} verknüpft")
        print(f"Issue #{number} angelegt – du bekommst eine Benachrichtigung von GitHub.")
    return new_state


def publish_draft(draft, state, now):
    """Veröffentlicht einen freigegebenen Entwurf und aktualisiert Entwurf, Protokoll und Issue."""
    url = public_raw_url(draft["image"])
    result = post_feed_image(url, draft["caption"])
    print(f"Instagram-Beitrag gepostet: {result}")
    draft["status"] = "published"
    draft["published_at"] = now.isoformat()
    new_state = record_post(state, now, "pool", extra={"id": draft["id"]})
    save_draft(draft)
    save_state(new_state)
    commit_paths([draft_json_path(draft["id"]), STATE_PATH], f"state: Pool-Post {draft['id']} gepostet")
    gh_comment(draft.get("issue_number"), "Gepostet. Knospi bedankt sich. 🌿")
    gh_close_issue(draft.get("issue_number"))
    return new_state


def expire_old_drafts(now, dry_run=False):
    """Entwürfe ohne Antwort (3 Tage) bzw. freigegebene, aber nie gepostete (7 Tage) verfallen."""
    for draft in load_drafts():
        created = _parse_iso(draft.get("created_at"))
        if created is None:
            continue
        age = now - created
        limit = DRAFT_REVIEW_DAYS if draft.get("status") == "draft" else DRAFT_EXPIRY_DAYS
        if draft.get("status") in ("draft", "approved") and age > timedelta(days=limit):
            print(f"Entwurf {draft['id']} ist verfallen ({draft['status']}, {age.days} Tage alt).")
            if dry_run:
                continue
            draft["status"] = "expired"
            save_draft(draft)
            commit_paths([draft_json_path(draft["id"])], f"draft: {draft['id']} verfallen")
            gh_comment(draft.get("issue_number"), "Verfallen: keine Antwort bzw. nicht gepostet. Kein Problem, es kommt ein neuer Vorschlag.")
            gh_close_issue(draft.get("issue_number"))


APPROVE_WORDS = {"ok", "okay", "ja", "freigeben", "freigabe", "passt", "yes", "👍"}
REJECT_WORDS = {"nein", "no", "verwerfen", "ablehnen", "stop", "👎"}


def handle_reply():
    """Wird vom Freigabe-Workflow aufgerufen, wenn du unter einem Entwurf-Issue kommentierst.
    'ok' gibt frei, 'nein' verwirft. Gepostet wird erst vom regulären Lauf (Tageslimit, Ruhezeit)."""
    comment = os.environ.get("COMMENT_BODY", "").strip().lower()
    issue_body = os.environ.get("ISSUE_BODY", "")
    number = int(os.environ.get("ISSUE_NUMBER", "0") or 0)
    match = re.search(r"knospi-draft:([\w\-]+)", issue_body)
    if not match:
        print("Kein Entwurf in diesem Issue gefunden.")
        return
    path = draft_json_path(match.group(1))
    if not path.exists():
        gh_comment(number, "Dieser Entwurf existiert nicht mehr im Repository.")
        return
    draft = json.loads(path.read_text(encoding="utf-8"))

    tokens = re.findall(r"[\wäöüß👍👎]+", comment)
    word = tokens[0] if tokens else ""
    if word not in APPROVE_WORDS | REJECT_WORDS:
        gh_comment(number, "Schreib `ok` zum Freigeben oder `nein` zum Verwerfen.")
        return
    if draft.get("status") != "draft":
        gh_comment(number, f"Dieser Entwurf ist schon '{draft.get('status')}'.")
        return

    if word in APPROVE_WORDS:
        draft["status"] = "approved"
        draft["approved_at"] = datetime.now(LOCAL_TZ).isoformat()
        save_draft(draft)
        commit_paths([path], f"draft: {draft['id']} freigegeben")
        gh_comment(number, "Freigegeben. Knospi postet bei der nächsten Gelegenheit "
                           f"(nicht zwischen {QUIET_START:%H}:{QUIET_START:%M} und {QUIET_END:%H}:{QUIET_END:%M} Uhr, "
                           "maximal ein Pool-Post pro Tag).")
    else:
        draft["status"] = "rejected"
        save_draft(draft)
        commit_paths([path], f"draft: {draft['id']} verworfen")
        gh_comment(number, "Verworfen. Dieser Inhalt kommt vorerst nicht wieder vor.")
        gh_close_issue(number)


# ---------------------------------------------------------------------------
# Hauptlogik
# ---------------------------------------------------------------------------

SENSOR_TAGS = {
    "tip": "#knospi #pflanzenliebe #pflanzenpflege",
    "improve": "#knospi #pflanzenliebe #danke",
    "daily": "#knospi #pflanzenliebe #pflanzenwg",
}


def sensor_caption(plan, reading):
    return (
        f"{plan['text']}\n\n"
        f"🌿 {PLANT_NAME} · {SPECIES_SHORT}\n"
        f"Erde {reading['soil']:.0f}% · Luft {reading['hum']:.0f}% · {reading['temp']:.1f}°C\n\n"
        f"{SENSOR_TAGS.get(plan['kind'], SENSOR_TAGS['daily'])}"
    )


def build_sensor_plans(state, readings, now):
    """Alle Sensor-Posts, die jetzt fachlich fällig wären, in Prioritätsreihenfolge
    (Danke vor 'wird schlecht' vor Hallo bei Problem). Gibt (pläne, reading, states) zurück."""
    reading = readings[-1]
    age = reading_age_hours(reading, now)
    if age is not None and age > MAX_READING_AGE_HOURS:
        print(f"Messwerte sind {age:.1f} Stunden alt (mehr als {MAX_READING_AGE_HOURS} h) – keine Sensor-Posts.")
        return [], reading, None

    states = evaluate(reading, state)
    events, suppressed = decide_events(state, states, now)
    if suppressed:
        print(f"Pause aktiv: kein neuer 'wird schlecht'-Post für {', '.join(suppressed)} "
              f"(weniger als {TIP_COOLDOWN_HOURS}h seit dem letzten Post zu diesem Wert).")

    confirmed = confirmed_states(readings, state)
    plans = []
    label_of = {"soil": "Erdfeuchte", "temp": "Temperatur", "hum": "Luftfeuchte"}
    improves, tips = [], []
    for kind, key, old, new in events:
        if confirmed[key] != states[key]:
            print(f"{label_of[key]}: Zustand '{new}' ist noch nicht stabil (Ausreißer?) – warte ab.")
            continue
        if kind == "tip":
            ok, why = tip_allowed(state, key, new, reading, now)
            if not ok:
                print(f"{label_of[key]} kippt ({old} -> {new}), aber: {why} – kein Post.")
                continue
            tips.append({"kind": "tip", "key": key, "text": tip_message(reading, key, new),
                         "label": f"{label_of[key]} kippt ({old} -> {new})"})
        else:
            improves.append({"kind": "improve", "key": key, "text": improve_message(reading, key, states),
                             "label": f"{label_of[key]} verbessert ({old} -> {new})"})
    plans += improves + tips

    problem = primary_problem(states)
    if problem and is_daily_due(state.get("last_daily"), now):
        key, st = problem
        if key == "hum" or state.get(key) == "ok":
            # Luft zu feucht (eigener Text) bzw. Zustand wurde noch nie gemeldet
            # (z.B. weil das Wochenlimit den 'wird schlecht'-Post zurückgehalten hat)
            text = tip_message(reading, key, st)
        else:
            text = hello_message(reading, states, thirsty_days(state, states, now))
        plans.append({"kind": "daily", "key": key, "state": st, "text": text,
                      "label": "täglicher Hallo-Post (es geht ihr gerade nicht gut)"})
    elif not problem and is_daily_due(state.get("last_daily"), now):
        print("Alles in Ordnung – kein täglicher Hallo-Post (der kommt nur, wenn es Knospi schlecht geht).")
    return plans, reading, states


def execute_sensor_plan(plan, state, reading, states, now, dry_run):
    image_path = render_story(reading, states, plan["text"])
    print(f"Story gerendert: {image_path} — {plan['label']}")
    caption = sensor_caption(plan, reading)
    if dry_run:
        print(f"(--dry-run) Kein echter Post, kein Commit. Caption:\n{caption}")
        return state

    # Nur das Bild committen - state.json bleibt bewusst unverändert, bis Instagram den Post
    # bestätigt hat. Schlägt etwas fehl, wurde nie 'erledigt' gespeichert -> nächster Lauf versucht es erneut.
    public_url = commit_and_get_public_url(image_path)
    print(f"Öffentliche Bild-URL: {public_url}")
    time.sleep(15)  # raw.githubusercontent.com braucht einen Moment, bis das Bild ausgeliefert wird
    result = post_feed_image(public_url, caption)
    print(f"Instagram-Beitrag gepostet: {result}")

    now_iso = now.isoformat()
    new_state = dict(state)
    new_state["last_event"] = dict(state.get("last_event") or {})
    if plan["kind"] == "daily":
        new_state["last_daily"] = now_iso
        new_state = record_post(new_state, now, "daily", plan.get("key"), plan.get("state"))
    else:
        key = plan["key"]
        new_state[key] = states[key]
        new_state["last_event"][key] = now_iso
        new_state = record_post(new_state, now, plan["kind"], key, states[key])
    save_state(new_state)
    commit_paths([STATE_PATH], f"state: {plan['label']}")
    return new_state


def main():
    args = sys.argv[1:]
    if "--handle-reply" in args:
        handle_reply()
        return
    if "--check-pool" in args:
        pool = load_pool()
        bad = 0
        for entry in pool:
            errors = validate_entry(entry)
            name = entry.get("id", "?") if isinstance(entry, dict) else "?"
            if errors:
                bad += 1
                print(f"FEHLER {name}: {'; '.join(errors)}")
            elif missing_assets(entry):
                print(f"wartet auf Assets {name}: {', '.join(str(p.relative_to(Path(__file__).parent)) for p in missing_assets(entry))}")
        print(f"{len(pool)} Einträge, {bad} fehlerhaft.")
        sys.exit(1 if bad else 0)

    dry_run = "--dry-run" in args
    force_draft = "--make-draft" in args
    now = datetime.now(LOCAL_TZ)

    state = load_state()
    # Altes state.json kann noch "hum": "low" enthalten (aus der Zeit, als trockene Luft ein
    # Problem war). Ohne diese Korrektur käme ein unsinniger "Luftfeuchte wieder gut"-Post.
    if not HUM_LOW_IS_PROBLEM and state.get("hum") == "low":
        state["hum"] = "ok"

    expire_old_drafts(now, dry_run)

    # 1) Sensor-Posts (Danke / wird schlecht / Hallo bei Problem) - nur wenn Daten da und aktuell sind
    plans, reading, states = [], None, None
    try:
        readings = fetch_readings(STABLE_READINGS)
    except Exception as e:
        print(f"WARNUNG: Sensordaten nicht abrufbar ({e}) – keine Sensor-Posts in diesem Lauf.")
        readings = []
    if readings:
        plans, reading, states = build_sensor_plans(state, readings, now)

    # 2) Freigegebener Pool-Entwurf hat Vorrang (von Hand freigegeben, zählt nicht zum Tageslimit
    #    der automatischen Posts, aber höchstens einer pro Tag und nie in der Ruhezeit)
    approved = [d for d in load_drafts() if d.get("status") == "approved"]
    posted = False
    if approved and not in_quiet_hours(now) and not pool_posts_today(state, now):
        draft = sorted(approved, key=lambda d: d.get("approved_at") or d.get("created_at") or "")[0]
        print(f"Freigegebener Entwurf wird gepostet: {draft['id']}")
        if dry_run:
            print(f"(--dry-run) Würde posten:\n{draft['caption']}")
        else:
            state = publish_draft(draft, state, now)
        posted = True
    elif approved:
        print("Freigegebener Entwurf wartet (Ruhezeit oder heute schon ein Pool-Post).")

    # 3) Genau ein automatischer Sensor-Post pro Lauf, wenn Ruhezeit und Tageslimit es erlauben
    if not posted and plans:
        allowed, why = automated_post_allowed(state, now)
        if allowed:
            state = execute_sensor_plan(plans[0], state, reading, states, now, dry_run)
            posted = True
        else:
            print(f"Sensor-Post '{plans[0]['label']}' wartet: {why}.")
    elif not posted and not approved:
        print("Keine Veränderung und kein Post fällig – nichts zu tun.")

    # 4) Neuer Pool-Entwurf zur Freigabe (Slot-Tag, Wochenlimit, kein offener Entwurf)
    if force_draft or pool_draft_due(state, now):
        state = create_pool_draft(state, now, dry_run)


if __name__ == "__main__":
    main()
