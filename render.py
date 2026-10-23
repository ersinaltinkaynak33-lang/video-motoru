"""MoneyPrinterTurbo sürücüsü: payload -> video -> (gerekirse sıkıştır) -> Telegram.

Cwd: MPT repo kökü (workflow `working-directory: mpt` ile çalıştırır).
Gerekli env: PAYLOAD, TG_TOKEN, CHAT_ID, PIXABAY_API_KEY
"""
import glob
import json
import os
import subprocess
import sys
import time

import requests

API = "http://127.0.0.1:8081/api/v1"
TG = f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}"
CHAT_ID = os.environ["CHAT_ID"]

# Telegram Bot API sınırı 50 MB; güvenlik payı bırak.
MAX_BYTES = 47 * 1024 * 1024
RENDER_DEADLINE_S = 15 * 60

# Worker ne gönderirse göndersin bunlar burada zorlanır.
# State değerleri MPT const.py'den: COMPLETE=1, FAILED=-1, PROCESSING=4
DEFAULTS = {
    "video_source": "pixabay",
    "voice_name": "tr-TR-EmelNeural-Female",
    "video_aspect": "9:16",
    "video_count": 1,
    "video_clip_duration": 4,
    "subtitle_enabled": True,
    # Varsayılan STHeitiMedium Türkçe karakterlerde (ş, ğ, İ) tofu verebilir; test et.
    "font_name": "MicrosoftYaHeiBold.ttc",
}


def notify(text):
    try:
        requests.post(f"{TG}/sendMessage", json={"chat_id": CHAT_ID, "text": text[:4000]}, timeout=30)
    except Exception as e:  # bildirim hatası işi düşürmesin
        print("notify failed:", e)


def write_config():
    key = os.environ["PIXABAY_API_KEY"]
    with open("config.toml", "w", encoding="utf-8") as f:
        f.write("[app]\n")
        f.write('video_source = "pixabay"\n')
        f.write(f"pixabay_api_keys = [{json.dumps(key)}]\n")
        f.write('subtitle_provider = "edge"\n')


def start_server():
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.asgi:app", "--host", "127.0.0.1", "--port", "8081"]
    )
    for _ in range(60):
        try:
            if requests.get(f"{API}/tasks", timeout=2).status_code == 200:
                return proc
        except requests.RequestException:
            pass
        time.sleep(1)
    proc.kill()
    raise RuntimeError("MPT sunucusu 60 sn içinde ayağa kalkmadı")


def probe(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", path]
    )
    j = json.loads(out)
    s = j["streams"][0]
    return int(s["width"]), int(s["height"]), float(j["format"]["duration"])


def shrink(src):
    """Hedef bitrate hesabıyla 47 MB altına in."""
    _, _, dur = probe(src)
    total_kbps = int(MAX_BYTES * 8 / dur / 1000 * 0.92)
    v_kbps = max(total_kbps - 128, 300)
    dst = src.replace(".mp4", "-small.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-i", src,
         "-c:v", "libx264", "-preset", "veryfast",
         "-b:v", f"{v_kbps}k", "-maxrate", f"{int(v_kbps * 1.2)}k", "-bufsize", f"{v_kbps * 2}k",
         "-vf", "scale=720:-2", "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", dst],
        check=True,
    )
    return dst


def main():
    payload = {**DEFAULTS, **json.loads(os.environ["PAYLOAD"])}
    payload.setdefault("video_subject", "video")
    write_config()
    server = start_server()
    try:
        r = requests.post(f"{API}/videos", json=payload, timeout=60)
        r.raise_for_status()
        task_id = r.json()["data"]["task_id"]
        print("task:", task_id)

        deadline = time.time() + RENDER_DEADLINE_S
        task = None
        while time.time() < deadline:
            time.sleep(10)
            task = requests.get(f"{API}/tasks/{task_id}", timeout=30).json().get("data", {})
            state = task.get("state")
            print("state:", state, "progress:", task.get("progress"))
            if state == 1:
                break
            if state == -1:
                raise RuntimeError("MPT görevi başarısız oldu (state=-1). Actions loguna bak.")
        else:
            raise TimeoutError(f"Render {RENDER_DEADLINE_S // 60} dakikada bitmedi")

        files = sorted(glob.glob(f"storage/tasks/{task_id}/final-*.mp4"))  # küçük harf 'storage'
        if not files:
            raise FileNotFoundError("final-*.mp4 bulunamadı")
        path = files[0]

        if os.path.getsize(path) > MAX_BYTES:
            print("Dosya büyük, sıkıştırılıyor:", os.path.getsize(path))
            path = shrink(path)
        if os.path.getsize(path) > MAX_BYTES:
            raise RuntimeError("Sıkıştırmadan sonra bile 50 MB altına inmedi")

        w, h, dur = probe(path)
        with open(path, "rb") as v:
            tr = requests.post(
                f"{TG}/sendVideo",
                data={
                    "chat_id": CHAT_ID,
                    "caption": payload["video_subject"][:1000],
                    "supports_streaming": "true",
                    "width": w, "height": h, "duration": int(dur),
                },
                files={"video": v},
                timeout=300,
            )
        if not tr.ok:
            raise RuntimeError(f"Telegram sendVideo hatası: {tr.status_code} {tr.text[:300]}")
    except Exception as e:
        notify(f"❌ Video üretilemedi: {e}")
        raise
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
