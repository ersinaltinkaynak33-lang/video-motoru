#!/usr/bin/env python3
"""
Video motoru (MPT'siz): Edge-TTS -> kelime zamanlı ASS -> Pixabay -> saf FFmpeg -> Telegram.

Akış:
  1. Edge-TTS sesi üretir (WordBoundary -> SentenceBoundary -> orantılı tahmin, 3 kademeli yedek).
  2. Ses loudnorm ile iki geçişte -14 LUFS'a normalize edilir.
  3. Her cümleye ait sahne, o cümlenin konuşulduğu süre aralığına yerleştirilir (ses-görüntü senkronu).
  4. Her sahne ~2 sn'lik hızlı kesimlere bölünür, 1080x1920'ye kırpılır.
     - Aynı Pixabay klibi (ID veya URL) bir video içinde ASLA iki kez kullanılmaz (used_video_ids).
     - Her Pixabay sorgusuna "cinematic realistic 4k" eklenir; 3D/CGI/neon etiketli klipler elenir.
  5. Tek geçişte ASS altyazı yakılır + normalize ses eklenir -> Telegram.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

import requests

W, H, FPS = 1080, 1920, 30
VOICE = os.environ.get("TTS_VOICE", "tr-TR-EmelNeural")
RATE = os.environ.get("TTS_RATE", "+8%")
TARGET_CUT = 2.0          # hedef kesim süresi (sn)
TAIL = 0.5                # ses bittikten sonra kalan görüntü (sn)
MAX_BYTES = 47 * 1024 * 1024
MAX_DOWNLOAD = 60 * 1024 * 1024
FONT_NAME = "DejaVu Sans"
FONT_FILES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]


def log(*a):
    print(*a, flush=True)


def run(cmd, cwd=None):
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, cwd=cwd)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} hata kodu {p.returncode}: {p.stderr[-900:]}")
    return p


def probe(path):
    """(video_stream_or_None, süre) döndürür."""
    p = run(["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path])
    j = json.loads(p.stdout)
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), None)
    return v, float(j.get("format", {}).get("duration") or 0)


# --------------------------------------------------------------------------- #
# 1. SES + KELİME ZAMANLARI
# --------------------------------------------------------------------------- #
async def _stream(text, boundary, mp3):
    import edge_tts
    com = edge_tts.Communicate(text, VOICE, rate=RATE, boundary=boundary)
    events = []
    with open(mp3, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] in ("WordBoundary", "SentenceBoundary"):
                events.append({"t": ch["text"],
                               "s": ch["offset"] / 1e7,
                               "e": (ch["offset"] + ch["duration"]) / 1e7})
    return events


def synth(text, mp3):
    """Ses üretir. edge-tts>=7 varsayılanı SentenceBoundary'dir; kelime için açıkça istenir."""
    last = None
    for attempt in range(3):
        try:
            kind = "word"
            ev = asyncio.run(_stream(text, "WordBoundary", mp3))
            if not ev:
                log("WordBoundary gelmedi, SentenceBoundary deneniyor.")
                kind = "sentence"
                ev = asyncio.run(_stream(text, "SentenceBoundary", mp3))
            if not Path(mp3).exists() or Path(mp3).stat().st_size < 2000:
                raise RuntimeError("Edge-TTS boş/çok küçük ses döndürdü")
            if not ev:
                kind = "none"
            return kind, ev
        except Exception as e:  # ağ, 403, NoAudioReceived...
            last = e
            log(f"Edge-TTS deneme {attempt + 1}/3 başarısız: {type(e).__name__}: {e}")
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Edge-TTS 3 denemede başarısız: {last}")


def tokens(s):
    return re.findall(r"\S+", s)


def build_words(kind, events, text, total):
    """Her durumda [{'t','s','e'}] kelime listesi üretir. Metin doluysa liste ASLA boş dönmez:
    WordBoundary eksik/seyrek gelirse cümle zamanlarına, o da yoksa toplam süreye orantılı dağıtılır."""
    n_text = len(tokens(text))
    if kind == "word":
        ws = [w for w in events if w["t"].strip()]
        if ws and len(ws) >= 0.6 * n_text:
            return ws
        log(f"    WordBoundary seyrek ({len(ws)}/{n_text}); orantılı zamanlamaya geçildi.")
        kind = "none"
    spans = ([(e["t"], e["s"], e["e"]) for e in events if e["t"].strip()]
             if kind == "sentence" else [])
    if kind == "sentence" and sum(len(tokens(t)) for t, _, _ in spans) < 0.6 * n_text:
        log("    SentenceBoundary eksik; orantılı zamanlamaya geçildi.")
        spans = []
    if not spans:
        spans = [(text, 0.0, total)]
    out = []
    for txt, s, e in spans:
        toks = tokens(txt)
        if not toks:
            continue
        weights = [len(t) + 1 for t in toks]
        tw = sum(weights)
        acc = s
        for t, wt in zip(toks, weights):
            d = (e - s) * wt / tw
            out.append({"t": t, "s": acc, "e": acc + d})
            acc += d
    return out


def normalize_audio(src, dst):
    """İki geçişli EBU R128 loudnorm (-14 LUFS). Ölçüm başarısızsa tek geçişe düşer."""
    base = "loudnorm=I=-14:TP=-1.5:LRA=11"
    af = base
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(src),
                        "-af", base + ":print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", p.stderr, re.S)
    if p.returncode == 0 and m:
        try:
            j = json.loads(m.group(0))
            vals = [float(j[k]) for k in ("input_i", "input_tp", "input_lra",
                                          "input_thresh", "target_offset")]
            af = (f"{base}:measured_I={vals[0]}:measured_TP={vals[1]}:measured_LRA={vals[2]}"
                  f":measured_thresh={vals[3]}:offset={vals[4]}:linear=true")
        except Exception as e:
            log("loudnorm ölçümü okunamadı, tek geçiş:", e)
    run(["ffmpeg", "-y", "-v", "error", "-i", src, "-af", af,
         "-ar", "48000", "-ac", "2", dst])


# --------------------------------------------------------------------------- #
# 2. ASS ALTYAZI
# --------------------------------------------------------------------------- #
def tr_upper(s):
    # Python upper() Türkçe i/ı'yı yanlış çevirir (i -> I). Elle düzelt.
    return s.replace("i", "İ").replace("ı", "I").upper()


def ass_time(t):
    cs = max(0, int(round(t * 100)))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def label_of(tok):
    tok = re.sub(r"[{}\\]", "", tok).strip(" .,!?;:…\"“”‘’()[]")
    return tr_upper(tok)


def chunk_words(words, max_words=3, max_chars=18):
    chunks, cur, chars = [], [], 0
    for w in words:
        lab = label_of(w["t"])
        if not lab:
            continue
        item = {**w, "lab": lab}
        if cur and (len(cur) >= max_words or chars + len(lab) + 1 > max_chars):
            chunks.append(cur)
            cur, chars = [], 0
        cur.append(item)
        chars += len(lab) + 1
        if re.search(r"[.!?…]$", w["t"].strip()):
            chunks.append(cur)
            cur, chars = [], 0
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(words, path, font=FONT_NAME):
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,8,5,2,60,60,520,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    chunks = chunk_words(words)
    lines = []
    for ci, ch in enumerate(chunks):
        nxt = chunks[ci + 1][0]["s"] if ci + 1 < len(chunks) else None
        for k, w in enumerate(ch):
            s = w["s"]
            if k + 1 < len(ch):
                e = ch[k + 1]["s"]
            else:
                e = w["e"] + 0.25
                if nxt is not None:
                    e = min(e, nxt)
            e = max(e, s + 0.06)
            parts = []
            for j, x in enumerate(ch):
                if j == k:   # aktif kelime: sarı + bounce (140% -> 100%, 170 ms)
                    parts.append("{\\1c&H00E5FF&\\fscx140\\fscy140\\t(0,170,\\fscx100\\fscy100)}"
                                 + x["lab"] + "{\\r}")
                else:
                    parts.append(x["lab"])
            lines.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Default,,0,0,0,,{' '.join(parts)}")
    Path(path).write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


# --------------------------------------------------------------------------- #
# 3. GÖRSEL MALZEME (Pixabay)
# --------------------------------------------------------------------------- #
PIXABAY_KEY = os.environ.get("PIXABAY_API_KEY", "")

# KURAL 3 (estetik): her Pixabay sorgusunun sonuna eklenir.
STYLE_SUFFIX = "cinematic realistic 4k"
STYLE_WORDS = {"cinematic", "realistic", "4k", "hd", "uhd", "8k"}

# Pixabay "film" filtresine rağmen sızan sahte/estetik uyumsuz içerik etiketleri -> bu etiketli klipler ELENİR
BAD_TAGS = {"3d", "render", "cgi", "cartoon", "animation", "animated", "neon", "cyberpunk",
            "abstract", "futuristic", "hologram", "fantasy", "illustration"}

# Sahne için hiç benzersiz klip bulunamazsa son çare (somut, jenerik görüntüler)
# DOĞA/MANZARA YOK: konu ev/teknoloji iken orman-okyanus-gün batımı görünmesin.
FALLBACK_QUERIES = ["living room interior", "kitchen counter hands", "person using smartphone",
                    "office desk laptop", "city street walking", "supermarket shopping aisle",
                    "coffee cup table", "people walking crosswalk"]
GENERIC_WORDS = {"of", "in", "on", "at", "to", "with", "and", "the", "a", "an", "holding", "using",
                 "close", "up", "view", "top"}

_PX_CACHE = {}


def _stem(w):
    w = w.lower()
    return w[:-1] if w.endswith("s") and len(w) > 3 else w


def styled(q):
    """Sorguya estetik son eki ekler (idempotent: önceden varsa tekrar eklemez)."""
    base = " ".join(w for w in q.split() if w.lower() not in STYLE_WORDS)
    return f"{base} {STYLE_SUFFIX}"[:100]


def rank_hits(hits, query, avoid):
    """Pixabay'in çıplak anahtar-kelime sonucunu etiketlerle doğrular.
    avoid: eşsesli/yanlış anlamı gösteren kelimeler (örn. tablet için pill, capsule) -> etiketlerde geçen sonuç ELENİR.
    BAD_TAGS (3D/CGI/neon...) etiketli sonuçlar da ELENİR.
    ALAKA KAPISI: etiketlerinde sorgunun en az bir içerik kelimesi geçmeyen klip ELENİR
    (Pixabay gevşek eşleşmede 'cinematic' vb. etiketli doğa klipleri döndürebilir)."""
    q = [_stem(w) for w in query.split() if w.lower() not in STYLE_WORDS]
    core = {w for w in q if w not in GENERIC_WORDS}      # alaka kapısı için içerik kelimeleri
    av = {_stem(w) for w in avoid}
    bad = {_stem(w) for w in BAD_TAGS}
    scored = []
    for i, h in enumerate(hits):
        tags = {_stem(t) for t in re.split(r"[,\s]+", str(h.get("tags", ""))) if t}
        if tags & av or tags & bad:
            continue
        if core and not (core & tags):                   # ALAKA KAPISI: etiketlerde sorgu kelimesi yoksa ELE
            continue
        if (h.get("duration") or 99) < 4:
            continue
        score = 2 * sum(1 for w in q if w in tags)
        scored.append((-score, i, h))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [h for _, _, h in scored]


def pixabay_raw(q):
    """Ham Pixabay sonucu. Aynı sorgu tek videoda iki kez API'ye gitmez; 429'da bekleyip tekrar dener."""
    if q in _PX_CACHE:
        return _PX_CACHE[q]
    for attempt in range(3):
        try:
            r = requests.get("https://pixabay.com/api/videos/",
                             params={"key": PIXABAY_KEY, "q": q[:100], "per_page": 50,
                                     "video_type": "film", "safesearch": "true"},
                             timeout=15)
            if r.status_code == 429:
                log(f"  Pixabay hız sınırı, {12 * (attempt + 1)} sn bekleniyor")
                time.sleep(12 * (attempt + 1))
                continue
            r.raise_for_status()
            hits = r.json().get("hits", [])
            _PX_CACHE[q] = hits
            return hits
        except Exception as e:
            log(f"  Pixabay '{q}' hata: {type(e).__name__}")
            time.sleep(1.5)
    return []


def pick_url(h):
    vs = h.get("videos", {})
    for name in ("large", "medium", "small"):
        v = vs.get(name) or {}
        if v.get("url") and (v.get("size") or 0) <= 35 * 1024 * 1024:
            return v["url"]
    return None


def pixabay_candidates(query, avoid, used_video_ids, limit=12):
    """[(video_id, url)] döndürür. used_video_ids içindeki ID/URL ASLA dönmez.
    Önce estetik ekli sorgu, sonra sade sorgu (estetik ek sonuçları boğarsa diye)."""
    if not PIXABAY_KEY:
        return []
    out, seen, skipped = [], set(), set()
    for q in (styled(query), query):
        for h in rank_hits(pixabay_raw(q), query, avoid):
            vid = str(h.get("id", ""))
            if not vid or vid in seen:
                continue                                   # pass
            url = pick_url(h)
            if vid in used_video_ids or (url and url in used_video_ids):
                skipped.add(vid)
                continue                                   # pass: bu video zaten kullanıldı
            if not url:
                continue                                   # pass
            seen.add(vid)
            out.append((vid, url))
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    if skipped:
        log(f"  atlandı (tekrar): {len(skipped)} kullanılmış klip")
    return out


def valid_video(path):
    try:
        v, dur = probe(path)
        return v is not None and dur >= 1.0
    except Exception:
        return False


def download(url, dest):
    try:
        with requests.get(url, stream=True, timeout=(10, 30)) as r:
            r.raise_for_status()
            size = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise RuntimeError("dosya çok büyük")
                    f.write(chunk)
    except Exception as e:
        log(f"  indirme hatası: {type(e).__name__}")
        Path(dest).unlink(missing_ok=True)
        return False
    if not valid_video(dest):          # JSON/HTML/bozuk dosyayı mp4 sanma
        log("  geçersiz video, atıldı")
        Path(dest).unlink(missing_ok=True)
        return False
    return True


def query_variants(q):
    """Tam sorgu, sonra 2 kelimelik daraltmalar. Tek kelimeye ASLA inmez (tek kelime = eşsesli/alakasız sonuç)."""
    w = q.split()
    out = [q]
    if len(w) > 2:
        out.append(" ".join(w[:2]))
        out.append(" ".join(w[-2:]))
    return list(dict.fromkeys(out))


def scene_queries(query, alt=""):
    """Sıra: tam sorgu, alternatif sorgu, sonra ikisinin 2 kelimelik daraltmaları."""
    out = [query]
    if alt:
        out.append(alt)
    out += query_variants(query)[1:]
    if alt:
        out += query_variants(alt)[1:]
    return list(dict.fromkeys(q for q in out if q))


def get_clips(idx, query, avoid, need, mdir, used_video_ids, alt=""):
    """Sahne için en fazla `need` BENZERSİZ klip indirir. used_video_ids tüm video boyunca ortaktır."""
    got = []
    for qv in scene_queries(query, alt):
        for vid, url in pixabay_candidates(qv, avoid, used_video_ids):
            if vid in used_video_ids or url in used_video_ids:
                log(f"  atlandı (tekrar): {vid}")
                continue                               # pass
            used_video_ids.add(vid)                    # indirme başarısız olsa bile tekrar denenmez
            used_video_ids.add(url)
            dest = mdir / f"s{idx:02d}_{len(got)}.mp4"
            if download(url, dest):
                got.append(dest)
                log(f"  + {vid}")
            if len(got) >= need:
                return got
    return got


# --------------------------------------------------------------------------- #
# 4. SAHNE ZAMANLAMASI + KESİM
# --------------------------------------------------------------------------- #
def load_scenes(payload):
    sc = payload.get("scenes")
    if sc:
        return [{"text": s["text"].strip(), "query": s["query"].strip(),
                 "alt": str(s.get("alt") or "").strip(), "avoid": list(s.get("avoid", []))}
                for s in sc if s.get("text")]
    # eski format yedeği: cümleleri sahnelere orantılı dağıt
    script = payload.get("video_script", "")
    sents = [x.strip() for x in re.split(r"(?<=[.!?…])\s+", script) if x.strip()]
    qs = [s.get("query", "") for s in payload.get("sahneler", [])] or ["city street"]
    groups = [[] for _ in qs]
    for i, s in enumerate(sents):
        groups[min(len(qs) - 1, i * len(qs) // max(1, len(sents)))].append(s)
    return [{"text": " ".join(g), "query": q, "avoid": []} for g, q in zip(groups, qs) if g]


def scene_spans(scenes, words, total):
    lens = [max(1, len(tokens(s["text"]))) for s in scenes]
    n, tot = len(words), sum(lens)
    bounds, cum = [0.0], 0
    for L in lens[:-1]:
        cum += L
        idx = min(n - 1, int(round(cum * n / tot)))
        bounds.append(max(bounds[-1] + 0.8, words[idx]["s"]))
    bounds.append(total)
    # son sahnelerin ezilmesini engelle
    for i in range(len(bounds) - 2, 0, -1):
        bounds[i] = min(bounds[i], bounds[i + 1] - 0.8)
    bounds[0] = 0.0
    return list(zip(bounds[:-1], bounds[1:]))


def make_segment(clip, off, frames, out):
    # kaynak klip kesimden kısaysa son kare çoğaltılır (tpad süresi kesim uzunluğundan büyük olmalı)
    pad = frames / FPS + 1
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,"
          f"fps={FPS},eq=contrast=1.05:saturation=1.06,tpad=stop_mode=clone:stop_duration={pad:.3f},format=yuv420p")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{off:.3f}", "-i", clip, "-vf", vf,
         "-frames:v", frames, "-an", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "18", "-r", FPS, out])


def build_video_track(scenes, spans, total, work):
    mdir = work / "materials"
    sdir = work / "segments"
    mdir.mkdir(exist_ok=True)
    sdir.mkdir(exist_ok=True)
    used_video_ids = set()          # TÜM video boyunca ortak: aynı klip (ID/URL) iki kez kullanılamaz

    clips_by_scene = {}
    fb = 0                          # FALLBACK_QUERIES için dönen sayaç
    for si, (sc, (a, b)) in enumerate(zip(scenes, spans)):
        want = min(4, max(1, int(round((b - a) / TARGET_CUT))))
        log(f"Sahne {si + 1}/{len(scenes)}: '{sc['query']}' ({want} klip)")
        clips = get_clips(si, sc["query"], sc.get("avoid", []), want, mdir, used_video_ids, sc.get("alt", ""))
        tries = 0
        while not clips and tries < len(FALLBACK_QUERIES):
            fq = FALLBACK_QUERIES[fb % len(FALLBACK_QUERIES)]
            fb += 1
            tries += 1
            log(f"  video yok, yedek sorgu: '{fq}'")
            clips = get_clips(si, fq, [], want, mdir, used_video_ids)
        if not clips:
            raise RuntimeError(f"Sahne {si + 1} için benzersiz video bulunamadı (Pixabay anahtarı/sorgular?)")
        clips_by_scene[si] = clips

    # Kesim planı: sahne içindeki kesim sayısı = benzersiz klip sayısı -> hiçbir klip tekrar etmez.
    # Klip az bulunduysa kesimler uzar (tpad kısa klibin son karesini uzatır); sahne süresi değişmez.
    plan = []                       # (sahne, sahne-içi indeks, bitiş zamanı)
    for si, (a, b) in enumerate(spans):
        nc = len(clips_by_scene[si])
        for j in range(nc):
            plan.append((si, j, a + (b - a) * (j + 1) / nc))

    end_frame_total = int(round(total * FPS))
    seg_files, prev_end_f = [], 0
    for pi, (si, j, t_end) in enumerate(plan):
        end_f = end_frame_total if pi == len(plan) - 1 else int(round(t_end * FPS))
        frames = max(2, end_f - prev_end_f)
        prev_end_f = end_f
        seg = sdir / f"seg_{pi:03d}.mp4"
        make_segment(clips_by_scene[si][j], 0.0, frames, seg)
        seg_files.append(seg)

    lst = work / "concat.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in seg_files), encoding="utf-8")
    track = work / "track.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
         "-c", "copy", track])
    n_unique = sum(len(c) for c in clips_by_scene.values())
    log(f"Görüntü hattı hazır: {len(seg_files)} kesim, {n_unique} benzersiz klip")
    assert n_unique == len(seg_files), "klip tekrarı tespit edildi"
    return track


# --------------------------------------------------------------------------- #
# 5. FİNAL BİRLEŞTİRME
# --------------------------------------------------------------------------- #
def prepare_fonts(work):
    fdir = work / "fonts"
    fdir.mkdir(exist_ok=True)
    for f in FONT_FILES:
        if Path(f).exists():
            shutil.copy(f, fdir / Path(f).name)
            return True
    log("UYARI: DejaVuSans-Bold bulunamadı; libass sistem fontuna düşecek (Türkçe karakterleri kontrol edin).")
    return False


def final_encode(track, audio, total, work, out, scale=None, vb=None):
    vf = "ass=subtitles.ass:fontsdir=fonts"
    if scale:
        vf += f",scale={scale}:-2"
    rate = ["-b:v", vb, "-maxrate", vb, "-bufsize", "8M"] if vb else \
           ["-crf", "22", "-maxrate", "7M", "-bufsize", "14M"]
    run(["ffmpeg", "-y", "-v", "error", "-i", track.name, "-i", audio.name,
         "-vf", vf, "-map", "0:v:0", "-map", "1:a:0", "-af", "apad",
         "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "veryfast", *rate,
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
         "-movflags", "+faststart", out], cwd=work)


def build(payload, work):
    """Tüm hattı çalıştırır, final mp4 yolunu ve süresini döndürür."""
    work.mkdir(parents=True, exist_ok=True)
    scenes = load_scenes(payload)
    if not scenes:
        raise RuntimeError("Payload'da sahne/senaryo yok")
    text = " ".join(s["text"] for s in scenes)
    if not tokens(text):
        raise RuntimeError("Senaryo metni boş")

    log("1/5 Ses üretiliyor (Edge-TTS)...")
    raw = work / "voice.mp3"
    kind, events = synth(text, raw)
    _, total_audio = probe(raw)
    words = build_words(kind, events, text, total_audio)
    log(f"    zamanlama kaynağı={kind}, kelime={len(words)}, süre={total_audio:.1f}s")
    if not words:
        raise RuntimeError("Kelime zamanlaması üretilemedi")

    log("2/5 Ses normalize ediliyor...")
    voice = work / "voice_ln.wav"
    normalize_audio(raw, voice)

    log("3/5 ASS altyazı yazılıyor...")
    prepare_fonts(work)
    n_ev = build_ass(words, work / "subtitles.ass")
    if n_ev == 0:
        raise RuntimeError("ASS dosyası boş çıktı")
    log(f"    {n_ev} altyazı olayı")

    log("4/5 Görseller ve kesimler...")
    total = total_audio + TAIL
    spans = scene_spans(scenes, words, total_audio)
    spans[-1] = (spans[-1][0], total)
    track = build_video_track(scenes, spans, total, work)

    log("5/5 Final render (altyazı + ses)...")
    out = work / "final.mp4"
    final_encode(track, voice, total, work, out.name)
    if out.stat().st_size > MAX_BYTES:
        log("    dosya büyük, 720p/yeniden kodlama...")
        vb = f"{max(800, int((MAX_BYTES * 8 / total - 192_000) / 1000 * 0.9))}k"
        out2 = work / "final_small.mp4"
        final_encode(track, voice, total, work, out2.name, scale=720, vb=vb)
        out = out2
    return out, total


# --------------------------------------------------------------------------- #
# TELEGRAM
# --------------------------------------------------------------------------- #
def main():
    token = os.environ["TG_TOKEN"]
    chat = os.environ["CHAT_ID"]
    tg = f"https://api.telegram.org/bot{token}"
    flag = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "render_notified.flag"
    flag.unlink(missing_ok=True)

    def redact(s):
        return str(s).replace(token, "***")

    try:
        payload = json.loads(os.environ["PAYLOAD"])
        title = payload.get("video_subject", "Video")
        out, total = build(payload, Path("work"))
        for attempt in range(2):
            with open(out, "rb") as f:
                r = requests.post(f"{tg}/sendVideo",
                                  data={"chat_id": chat, "caption": title[:1000],
                                        "supports_streaming": "true",
                                        "width": 1080 if out.name == "final.mp4" else 720,
                                        "height": 1920 if out.name == "final.mp4" else 1280,
                                        "duration": int(total)},
                                  files={"video": f}, timeout=300)
            if r.ok and r.json().get("ok"):
                log("Telegram'a gönderildi.")
                return
            log("Telegram reddetti:", redact(r.text[:300]))
            time.sleep(3)
        raise RuntimeError(f"Telegram sendVideo reddedildi: {redact(r.text[:300])}")
    except Exception as e:
        log(redact(traceback.format_exc()))
        run_url = "{}/{}/actions/runs/{}".format(
            os.environ.get("GITHUB_SERVER_URL", ""), os.environ.get("GITHUB_REPOSITORY", ""),
            os.environ.get("GITHUB_RUN_ID", ""))
        try:
            requests.post(f"{tg}/sendMessage", json={
                "chat_id": chat,
                "text": f"❌ Video üretilemedi: {redact(type(e).__name__)}: {redact(e)[:600]}\n{run_url}"},
                timeout=30)
            flag.write_text("1")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
