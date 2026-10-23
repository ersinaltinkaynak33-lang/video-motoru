"""MoneyPrinterTurbo Sürücüsü - 4 Madde Entegre (Ses, Görsel, ASS, Kaynak)"""
import os, sys, json, time, glob, subprocess, asyncio, urllib.parse, requests
import edge_tts

API = "http://127.0.0.1:8081/api/v1"
TG = f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}"
CHAT_ID = os.environ["CHAT_ID"]
PIXABAY_KEY = os.environ.get("PIXABAY_API_KEY", "")
MAX_BYTES = 47 * 1024 * 1024
RENDER_DEADLINE_S = 15 * 60

async def generate_audio_and_ass(text, voice, mp3_path, ass_path):
    print("Ses ve ASS Altyazı üretiliyor...")
    communicate = edge_tts.Communicate(text, voice, rate="+10%")
    words = []
    with open(mp3_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append({
                    "word": chunk["text"],
                    "start": chunk["offset"] / 10000000,
                    "duration": chunk["duration"] / 10000000
                })
    
    # ASS Formatı (Hormozi Tarzı - Kelime Kelime Vurgulu)
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 1
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,100,&H00FFFF&,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,8,3,2,10,10,400,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        for i, w in enumerate(words):
            start = w["start"]
            end = start + w["duration"]
            # Zamanı ASS formatına çevir (H:MM:SS.cs)
            def fmt(t): return f"{int(t//3600)}:{int((t%3600)//60):02d}:{int(t%60):02d}.{int((t%1)*100):02d}"
            # Kelimeye pop-in efekti ekle
            text_escaped = w["word"].replace("{", "\\{").replace("}", "\\}")
            f.write(f"Dialogue: 0,{fmt(start)},{fmt(end)},Default,,0,0,0,,{{\\fscx120\\fscy120\\t(0,100,\\fscx100\\fscy100)}}{text_escaped}\n")
    print(f"Altyazı kaydedildi: {len(words)} kelime")

def download_materials(sahneler):
    print("Çoklu kaynaktan görseller indiriliyor...")
    os.makedirs("local_materials", exist_ok=True)
    paths = []
    for i, sahne in enumerate(sahneler):
        q = urllib.parse.quote(sahne["query"])
        kaynak = sahne["kaynak"]
        url = ""
        
        if kaynak == "pixabay" and PIXABAY_KEY:
            # Pixabay araması
            api = f"https://pixabay.com/api/videos/?key={PIXABAY_KEY}&q={q}&per_page=5&min_width=720"
            r = requests.get(api, timeout=10).json()
            if r.get("hits"):
                url = r["hits"][0]["videos"]["medium"]["url"]
        else:
            # Coverr veya Pixabay yoksa Wikimedia'dan dene
            url = f"https://commons.wikimedia.org/w/api.php?action=query&generator=search&gsrsearch={q}+filetype:video&gsrlimit=1&prop=imageinfo&iiprop=url&format=json"
            r = requests.get(url, timeout=10, headers={"User-Agent": "Bot/1.0"}).json()
            try:
                pages = r["query"]["pages"]
                for p in pages:
                    url = pages[p]["imageinfo"][0]["url"]
            except: pass

        if url:
            v_path = f"local_materials/sahne_{i}.mp4"
            try:
                vid = requests.get(url, timeout=15)
                with open(v_path, "wb") as f: f.write(vid.content)
                paths.append(v_path)
                print(f"Sahne {i+1} indirildi: {kaynak}")
            except: print(f"Sahne {i+1} indirilemedi.")
        else:
            print(f"Sahne {i+1} için video bulunamadı, atlanıyor.")
    
    return paths

def start_server():
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.asgi:app", "--host", "127.0.0.1", "--port", "8081"])
    for _ in range(60):
        try:
            if requests.get(f"{API}/tasks", timeout=2).status_code == 200: return proc
        except: pass
        time.sleep(1)
    proc.kill()
    raise RuntimeError("MPT başlamadı")

def normalize(src):
    dst = src.replace(".mp4", "-ln.mp4")
    subprocess.run(["ffmpeg", "-y", "-i", src, "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", dst], check=True)
    return dst

def burn_subtitles(src, ass):
    dst = src.replace(".mp4", "-final.mp4")
    # ASS dosyasını videoya göm
    subprocess.run(["ffmpeg", "-y", "-i", src, "-vf", f"ass={ass}:fontsdir=assets/fonts", "-c:a", "copy", dst], check=True)
    return dst

def main():
    payload = json.loads(os.environ["PAYLOAD"])
    sahneler = payload.get("sahneler", [])
    senaryo = payload.get("video_script", "")
    baslik = payload.get("video_subject", "Video")

    # 1. Ses ve ASS Altyazı
    audio_path = "audio.mp3"
    ass_path = "subtitles.ass"
    asyncio.run(generate_audio_and_ass(senaryo, "tr-TR-EmelNeural", audio_path, ass_path))

    # 2. Görselleri İndir
    material_paths = download_materials(sahneler)

    # 3. Config Yaz
    with open("config.toml", "w", encoding="utf-8") as f:
        f.write("[app]\nvideo_source = \"local\"\nsubtitle_provider = \"none\"\n")

    server = start_server()
    try:
        # 4. MPT'yi Çalıştır
        mpt_payload = {
            "video_subject": baslik,
            "video_script": senaryo,
            "video_source": "local",
            "video_materials": material_paths,
            "custom_audio_file": os.path.abspath(audio_path),
            "subtitle_enabled": False,
            "video_aspect": "9:16",
            "video_clip_duration": 2,
            "video_concat_mode": "sequential",
            "voice_name": "tr-TR-EmelNeural" # MPT bunu kullanmayacak ama hata vermesin
        }
        r = requests.post(f"{API}/videos", json=mpt_payload, timeout=60)
        task_id = r.json()["data"]["task_id"]

        deadline = time.time() + RENDER_DEADLINE_S
        while time.time() < deadline:
            time.sleep(10)
            task = requests.get(f"{API}/tasks/{task_id}", timeout=30).json().get("data", {})
            if task.get("state") == 1: break
            if task.get("state") == -1: raise RuntimeError("MPT çöktü.")
        
        files = sorted(glob.glob(f"storage/tasks/{task_id}/final-*.mp4"))
        if not files: raise FileNotFoundError("final.mp4 bulunamadı")

        # 5. Post-Process (Ses Norm + ASS Gömme)
        print("Ses normalize ediliyor ve altyazı gömülüyor...")
        norm_path = normalize(files[0])
        final_path = burn_subtitles(norm_path, os.path.abspath(ass_path))

        # 6. Telegram'a Gönder
        if os.path.getsize(final_path) > MAX_BYTES:
            print("Dosya büyük, sıkıştırılıyor...")
            subprocess.run(["ffmpeg", "-y", "-i", final_path, "-c:v", "libx264", "-preset", "veryfast", "-b:v", "2000k", "-vf", "scale=720:-2", "-c:a", "aac", "-b:a", "128k", "final-small.mp4"], check=True)
            final_path = "final-small.mp4"

        with open(final_path, "rb") as v:
            requests.post(f"{TG}/sendVideo", data={"chat_id": CHAT_ID, "caption": baslik[:1000], "supports_streaming": "true"}, files={"video": v}, timeout=300)
            
    except Exception as e:
        requests.post(f"{TG}/sendMessage", json={"chat_id": CHAT_ID, "text": f"❌ Video üretilemedi: {e}"})
        raise
    finally:
        server.terminate()

if __name__ == "__main__":
    main()
