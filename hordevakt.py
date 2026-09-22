"""
Hordevakt – lytter på Hordejakten-livestreamen og varsler om hint.

Hva den gjør, hele tiden mens den kjører:
  * henter lyden fra YouTube-strømmen i 30-sekunders biter
  * tale  -> tekst (faster-whisper, norsk)  -> varsel ved nøkkelord
  * fuglesang -> art (BirdNET)
  * andre lyder (dyr, vann, klokker, tog, båt ...) -> YAMNet
  * sender varsler til mobilen via ntfy.sh
  * sender et sammendrag av alt som er sagt hvert 15. minutt

Innstillinger kommer fra miljøvariabler (settes i GitHub):
  NTFY_TOPIC     – navnet på ntfy-kanalen din (påkrevd for varsler)
  STREAM_URL     – valgfri, tvinger en bestemt YouTube-lenke
  KJORETID_MIN   – hvor mange minutter skriptet skal kjøre (standard 345)
  TEST           – "1" = kort test: sjekk tilgang, analyser én bit, send testvarsel
"""

import os
import re
import sys
import time
import shutil
import signal
import threading
import subprocess
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ----------------------------------------------------------------- oppsett
HORDE_SIDE = "https://horde.no/gjeldfri/hordejakten"
STANDARD_VIDEO = "https://www.youtube.com/watch?v=EQHgfmZicc8"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
KJORETID = float(os.environ.get("KJORETID_MIN", "345")) * 60
TEST = os.environ.get("TEST", "") == "1"
BIT_SEK = 30
ARBEID = Path("arbeid")
LOGG = Path("logg.txt")
SAMMENDRAG_HVERT = 15 * 60

# Oslo-tid uten ekstra biblioteker (sommertid grovt håndtert via zoneinfo hvis mulig)
try:
    from zoneinfo import ZoneInfo
    OSLO = ZoneInfo("Europe/Oslo")
except Exception:  # pragma: no cover
    OSLO = timezone(timedelta(hours=2))


def naa():
    return datetime.now(OSLO).strftime("%H:%M:%S")


def logg(tekst):
    linje = f"[{naa()}] {tekst}"
    with LOGG.open("a", encoding="utf-8") as f:
        f.write(linje + "\n")
    # Skriv bare tekniske meldinger til konsollen, ikke selve funnene,
    # slik at de ikke blir synlige i offentlige GitHub-logger.


def status(tekst):
    print(f"[{naa()}] {tekst}", flush=True)


def les_nokkelord():
    fil = Path(__file__).with_name("nokkelord.txt")
    ord_ = []
    if fil.exists():
        for l in fil.read_text(encoding="utf-8").splitlines():
            l = l.strip()
            if l and not l.startswith("#"):
                ord_.append(l.lower())
    return ord_


NOKKELORD = les_nokkelord()
# Kode-lignende ting: to bokstaver + tall (LD67), GPS-lignende tall osv.
KODE_MONSTER = re.compile(r"\b([A-ZÆØÅ]{1,3}\s?-?\d{1,4})\b")
TALL_MONSTER = re.compile(r"\b\d{2,}\b")


# ----------------------------------------------------------------- varsling
def varsle(tittel, tekst, prioritet="default", tags=""):
    if not NTFY_TOPIC:
        return
    data = tekst[:3900].encode("utf-8")
    req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}", data=data, method="POST")
    # Titler må være ASCII-trygge i HTTP-headere; ntfy støtter RFC 2047-koding
    req.add_header("Title", "=?UTF-8?B?" + __import__("base64").b64encode(tittel.encode()).decode() + "?=")
    req.add_header("Priority", prioritet)
    if tags:
        req.add_header("Tags", tags)
    for forsok in range(3):
        try:
            urllib.request.urlopen(req, timeout=15).read()
            return
        except Exception as e:
            status(f"ntfy-feil ({forsok + 1}/3): {e}")
            time.sleep(3)


# ----------------------------------------------------------------- strømmen
def finn_stream_url():
    if os.environ.get("STREAM_URL", "").strip():
        return os.environ["STREAM_URL"].strip()
    try:
        html = urllib.request.urlopen(
            urllib.request.Request(HORDE_SIDE, headers={"User-Agent": "Mozilla/5.0"}), timeout=20
        ).read().decode("utf-8", "ignore")
        m = re.search(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=|live/)|youtu\.be/)([\w-]{11})", html)
        if m:
            return f"https://www.youtube.com/watch?v={m.group(1)}"
    except Exception as e:
        status(f"Fant ikke video på Horde-siden ({e}), bruker standard.")
    return STANDARD_VIDEO


def hent_hls(url):
    cmd = ["yt-dlp", "-g", "-f", "91/92/93/94/bestaudio/best", "--no-warnings"]
    if Path("cookies.txt").exists() and Path("cookies.txt").stat().st_size > 0:
        cmd += ["--cookies", "cookies.txt"]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(r.stderr.strip()[-800:] or "yt-dlp ga ingen adresse")
    return r.stdout.strip().splitlines()[0]


def opptaker(stopp: threading.Event, slutt_tid):
    """Tar opp lyd kontinuerlig i 30-sek WAV-filer. Starter på nytt ved brudd."""
    ARBEID.mkdir(exist_ok=True)
    runde = 0
    feil_varslet = False
    while not stopp.is_set() and time.time() < slutt_tid:
        runde += 1
        try:
            url = finn_stream_url()
            hls = hent_hls(url)
            status(f"Kobler til strømmen (runde {runde})")
            feil_varslet = False
        except Exception as e:
            status(f"Klarte ikke hente strømmen: {e}")
            if not feil_varslet:
                varsle("Hordevakt: får ikke tak i strømmen",
                       f"Prøver igjen hvert minutt.\n\n{str(e)[:600]}", "high", "warning")
                feil_varslet = True
            stopp.wait(60)
            continue
        monster = str(ARBEID / f"r{runde:03d}_%05d.wav")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if hls.startswith("http"):
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
        cmd += ["-i", hls, "-vn", "-ac", "1", "-ar", "48000",
               "-f", "segment", "-segment_time", str(BIT_SEK), "-reset_timestamps", "1", monster]
        p = subprocess.Popen(cmd)
        while p.poll() is None and not stopp.is_set() and time.time() < slutt_tid:
            time.sleep(2)
        if p.poll() is None:
            p.send_signal(signal.SIGINT)
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        else:
            status("Strømmen brøt, kobler til på nytt om 15 s")
            stopp.wait(15)


# ----------------------------------------------------------------- analyse
class Analyse:
    INTERESSANTE_YAMNET = None  # settes i __init__

    def __init__(self):
        status("Laster modeller (tar et par minutter første gang) ...")
        from faster_whisper import WhisperModel
        self.whisper = WhisperModel(os.environ.get("WHISPER_MODELL", "small"),
                                    device="cpu", compute_type="int8")

        import tensorflow_hub as hub
        import csv
        self.yamnet = hub.load("https://tfhub.dev/google/yamnet/1")
        sti = self.yamnet.class_map_path().numpy().decode()
        with open(sti, encoding="utf-8") as f:
            self.yam_navn = [r["display_name"] for r in csv.DictReader(f)]

        from birdnetlib.analyzer import Analyzer
        self.birdnet = Analyzer()

        # Lyder vi ignorerer (for vanlige til å si noe om stedet)
        self.ignorer = {
            "Speech", "Silence", "Music", "Inside, small room", "Inside, large room or hall",
            "Male speech, man speaking", "Female speech, woman speaking", "Conversation",
            "Narration, monologue", "Speech synthesizer", "Child speech, kid speaking",
            "Noise", "Static", "White noise", "Pink noise", "Hum", "Mains hum",
            "Background music", "Musical instrument", "Sound effect", "Television", "Radio",
            "Echo", "Reverberation", "Environmental noise", "Outside, rural or natural",
            "Outside, urban or manmade", "Breathing", "Cough", "Sniff", "Throat clearing",
            "Clicking", "Tick", "Rustle", "Rub", "Scratch",
        }
        self.sist_sett = {}          # etikett -> tidspunkt, for å unngå spam
        self.nedkjoling = 10 * 60
        status("Modeller lastet.")

    def _ny(self, noekkel):
        t = time.time()
        if t - self.sist_sett.get(noekkel, 0) > self.nedkjoling:
            self.sist_sett[noekkel] = t
            return True
        return False

    def tale(self, wav):
        segs, _ = self.whisper.transcribe(
            str(wav), language="no", vad_filter=True, beam_size=1,
            condition_on_previous_text=False)
        tekst = " ".join(s.text.strip() for s in segs if s.no_speech_prob < 0.6).strip()
        # Whisper finner av og til på fraser i stillhet
        if tekst.lower() in {"teksting av nicolai winther", "takk for meg.", "takk.", "..."}:
            return ""
        return tekst

    def lyder(self, wav):
        import numpy as np
        import librosa
        y, _ = librosa.load(str(wav), sr=16000, mono=True)
        if len(y) < 16000:
            return []
        scores, _, _ = self.yamnet(y.astype(np.float32))
        snitt = scores.numpy().max(axis=0)   # sterkeste forekomst i biten
        funn = []
        for i in np.argsort(snitt)[::-1][:8]:
            navn = self.yam_navn[i]
            if snitt[i] >= 0.25 and navn not in self.ignorer:
                funn.append((navn, float(snitt[i])))
        return funn

    def fugler(self, wav):
        from birdnetlib import Recording
        rec = Recording(self.birdnet, str(wav), lat=61.0, lon=9.5,
                        date=datetime.now(), min_conf=0.45)
        rec.analyze()
        beste = {}
        for d in rec.detections:
            navn = f"{d['common_name']} ({d['scientific_name']})"
            beste[navn] = max(beste.get(navn, 0), d["confidence"])
        return sorted(beste.items(), key=lambda x: -x[1])


def sjekk_nokkelord(tekst):
    lav = tekst.lower()
    treff = [o for o in NOKKELORD if re.search(r"(?<!\w)" + re.escape(o), lav)]
    koder = [k for k in KODE_MONSTER.findall(tekst) if any(c.isdigit() for c in k)]
    return treff, koder


# ----------------------------------------------------------------- hovedløkke
def main():
    start = time.time()
    slutt = start + (6 * 60 if TEST else KJORETID)
    status(f"Hordevakt starter. Kjører til {datetime.fromtimestamp(slutt, OSLO):%H:%M}. "
           f"Nøkkelord: {len(NOKKELORD)}. Varsler: {'på' if NTFY_TOPIC else 'AV (mangler NTFY_TOPIC)'}")
    if ARBEID.exists():
        shutil.rmtree(ARBEID)
    ARBEID.mkdir()

    # Rask tilgangssjekk før vi laster tunge modeller
    url = finn_stream_url()
    try:
        hent_hls(url)
        status(f"Tilgang til strømmen: OK ({url})")
    except Exception as e:
        status(f"TILGANG FEILET: {e}")
        varsle("Hordevakt: YouTube stopper GitHub",
               "Strømmen kunne ikke hentes. Se veiledningen: legg inn YT_COOKIES.\n\n" + str(e)[:1500],
               "high", "warning")
        if TEST:
            sys.exit(1)

    analyse = Analyse()
    stopp = threading.Event()
    t = threading.Thread(target=opptaker, args=(stopp, slutt), daemon=True)
    t.start()

    varsle("Hordevakt er i gang" + (" (TEST)" if TEST else ""),
           f"Lytter på {url}\nKjører til ca. {datetime.fromtimestamp(slutt, OSLO):%H:%M}.",
           "low", "ear")

    behandlet = set()
    tale_buffer = []
    siste_sammendrag = time.time()
    antall_biter = 0

    while time.time() < slutt + BIT_SEK or t.is_alive():
        filer = sorted(ARBEID.glob("*.wav"))
        # Siste fil skrives fortsatt av ffmpeg -> hopp over den så lenge opptaket går
        ferdige = filer[:-1] if t.is_alive() else filer
        nye = [f for f in ferdige if f.name not in behandlet]
        if not nye:
            if not t.is_alive():
                break
            time.sleep(3)
            continue
        # Hvis vi henger etter, analyser de nyeste først for tale og hopp over for gamle
        if len(nye) > 6:
            status(f"Henger etter ({len(nye)} biter), hopper over de eldste")
            for f in nye[:-3]:
                behandlet.add(f.name)
                f.unlink(missing_ok=True)
            nye = nye[-3:]

        for wav in nye:
            behandlet.add(wav.name)
            tid = naa()
            try:
                tekst = analyse.tale(wav)
                if tekst:
                    logg(f"TALE: {tekst}")
                    tale_buffer.append(f"{tid[:5]} {tekst}")
                    treff, koder = sjekk_nokkelord(tekst)
                    if treff or koder:
                        sett, liste = set(), []
                        for x in treff + koder:
                            k = x.lower().replace(" ", "").replace("-", "")
                            if k not in sett:
                                sett.add(k); liste.append(x)
                        hva = ", ".join(liste)
                        varsle(f"Mulig hint: {hva}", f"{tid}\n«{tekst}»", "high", "rotating_light")
                        logg(f"VARSEL nøkkelord: {hva}")

                for navn, s in analyse.fugler(wav):
                    logg(f"FUGL: {navn} {s:.0%}")
                    if analyse._ny("fugl:" + navn):
                        varsle(f"Fugl hørt: {navn.split(' (')[0]}",
                               f"{tid} – {navn}, sikkerhet {s:.0%}", "default", "bird")

                for navn, s in analyse.lyder(wav):
                    logg(f"LYD: {navn} {s:.0%}")
                    if analyse._ny("lyd:" + navn):
                        varsle(f"Lyd: {navn}", f"{tid} – sikkerhet {s:.0%}", "default", "loud_sound")
            except Exception as e:
                status(f"Analysefeil i {wav.name}: {e}")
            finally:
                wav.unlink(missing_ok=True)
                antall_biter += 1

        if tale_buffer and (time.time() - siste_sammendrag > SAMMENDRAG_HVERT or TEST):
            varsle("Sagt i strømmen siste kvarter", "\n".join(tale_buffer), "low", "speech_balloon")
            tale_buffer = []
            siste_sammendrag = time.time()

        if antall_biter and antall_biter % 20 == 0:
            status(f"{antall_biter} biter analysert")

    stopp.set()
    if tale_buffer:
        varsle("Sagt i strømmen (siste)", "\n".join(tale_buffer), "low", "speech_balloon")
    status(f"Ferdig. {antall_biter} biter analysert.")
    if TEST:
        if antall_biter:
            varsle("Hordevakt-test ferdig", f"Testen virket. {antall_biter} lydbiter analysert.", "default", "white_check_mark")
        else:
            varsle("Hordevakt-test: ingen lyd", "Fikk kontakt, men ingen lyd ble tatt opp. Er strømmen live nå?", "high", "warning")


if __name__ == "__main__":
    main()
