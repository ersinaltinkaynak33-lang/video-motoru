"""MoneyPrinterTurbo Sürücüsü - Nihai Cerrahi Müdahale (v2)"""
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
    sentence_boundaries = []
    
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
            elif chunk["type"] == "SentenceBoundary":
                sentence_boundaries.append({
                    "text": chunk["text"],
                    "start": chunk["offset"] / 10000000,
                    "duration": chunk["duration"] / 10000000
                })
    
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 1
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,100,&H00FFFF&,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,8,3,2,10,10,450,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    
    def fmt(t): return f"{int(t//3600)}:{int((t%3600)//60):02d}:{int(t%60):02d}.{int((t%1)*100):02d}"
    
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        if not words and sentence_boundaries:
            print("Uyarı: Kelime zamanlaması alınamadı, cümle bazlı altyazı üretiliyor.")
            for sb in sentence_boundaries:
                start, end = sb["start"], sb["start"] + sb["duration"]
                text_escaped = sb["text"].replace("{", "\\{").replace("}", "\\}")
                f.write(f"Dialogue: 0,{fmt(start)},{fmt(end)},Default,,0,0,0,,{text_escaped}\n")
        elif words:
            print(f"Altyazı kaydedildi: {len(words)} kelime")
            for w in words:
                start, end = w["start"], w["start"] + w["duration"]
                text_escaped = w["word"].replace("{", "\\{").replace("}", "\\}")
                f.write(f"Dialogue: 0,{fmt(start)},{fmt(end)},Default,,0,0,0,,{{\\fscx120\\fscy120\\t(0,100,\\fscx100\\fscy100)}}{text_escaped}\n")
        else:
            print("Kritik Uyarı: Hiçbir zamanlama verisi alınamadı, altyazısız devam ediliyor.")

def download_materials(sahneler):
    print("Çoklu kaynaktan görseller indiriliyor...")
    os.makedirs("local_materials", exist_ok=True)
    paths = []
    for i, sahne in enumerate(sahneler):
        q = urllib.parse.quote(sahne["query"])
        kaynak = sahne["kaynak"]
        url = ""
        
        # Pixabay'den arama
        if PIXABAY_KEY:
            api = f"https://pixabay.com/api/videos/?key={PIXABAY_KEY}&q={q}&per_page=5&min_width=720"
            try:
                r = requests.get(api, timeout=10).json()
                if r.get("hits"):
                    url = r["hits"][0]["videos"]["medium"]["url"]
            except: pass

        if url:
            v_path = f"local_materials/sahne_{i}.mp4"
            try:
                vid = requests.get(url, timeout=15, stream=True)
                content_type = vid.headers.get("Content-Type", "")
                if "video" not in content_type and "octet-stream" not in content_type:
                    print(f"Sahne {i+1} geçersiz format ({content_type}), atlanıyor.")
                    continue
                    
                with open(v_path, "wb") as f:
                    for chunk in vid.iter_content(chunk_size=8192):
                        f.write(chunk)
                paths.append(v_path)
                print(f"Sahne {i+1} indirildi: {kaynak}")
            except Exception as e: 
                print(f"Sahne {i+1} indirilemedi: {e}")
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
    subprocess.run(["ffmpeg", "-y", "-i", src, "-vf", f"ass={ass}:fontsdir=assets/fonts", "-c:a", "copy", dst], check=True)
    return dst

def main():
    payload = json.loads(os.environ["PAYLOAD"])
    sahneler = payload.get("sahneler", [])
    senaryo = payload.get("video_script", "")
    baslik = payload.get("video_subject", "Video")

    audio_path = "audio.mp3"
    ass_path = "subtitles.ass"
    asyncio.run(generate_audio_and_ass(senaryo, "tr-TR-EmelNeural", audio_path, ass_path))

    material_paths = download_materials(sahneler)

    if not material_paths:
        raise RuntimeError("Hiçbir görsel indirilemedi, video üretilemez.")

    with open("config.toml", "w", encoding="utf-8") as f:
        f.write("[app]\nvideo_source = \"local\"\nsubtitle_provider = \"none\"\n")

    server = start_server()
    try:
        # GÜNCELLEME BURADA: video_materials artık sözlük listesi
        formatted_materials = [{"type": "video", "path": os.path.abspath(p)} for p in material_paths]
        
        mpt_payload = {
            "video_subject": baslik,
            "video_script": senaryo,
            "video_source": "local",
            "video_materials": formatted_materials, # DÜZELTİLDİ
            "custom_audio_file": os.path.abspath(audio_path),
            "subtitle_enabled": False,
            "video_aspect": "9:16",
            "video_clip_duration": 2,
            "video_concat_mode": "sequential",
            "voice_name": "tr-TR-EmelNeural"
        }
        
        print("MPT'ye video isteği gönderiliyor...")
        r = requests.post(f"{API}/videos", json=mpt_payload, timeout=60)
        
        if r.status_code != 200:
            print(f"MPT API HATASI ({r.status_code}): {r.text}")
            raise RuntimeError(f"MPT isteği reddetti: {r.text}")
            
        resp_json = r.json()
        task_id = resp_json["data"]["task_id"]
        print(f"Görev alındı: {task_id}")

        deadline = time.time() + RENDER_DEADLINE_S
        while time.time() < deadline:
            time.sleep(10)
            task = requests.get(f"{API}/tasks/{task_id}", timeout=30).json().get("data", {})
            if task.get("state") == 1: break
            if task.get("state") == -1: raise RuntimeError("MPT çöktü (state=-1).")
        
        files = sorted(glob.glob(f"storage/tasks/{task_id}/final-*.mp4"))
        if not files: raise FileNotFoundError("final.mp4 bulunamadı")

        print("Ses normalize ediliyor ve altyazı gömülüyor...")
        norm_path = normalize(files[0])
        
        if os.path.getsize(ass_path) > 500:
            final_path = burn_subtitles(norm_path, os.path.abspath(ass_path))
        else:
            final_path = norm_path

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
