# video-motoru

Telegram üzerinden alınan bir konu isteğini, otomatik olarak **dikey (1080×1920) TikTok / Reels / Shorts formatında videoya** dönüştüren tam otomasyon hattı.

## 🎯 Proje Ne Yapıyor?

Kullanıcı Telegram'da `/yeni <konu>` yazdığında:

1. **Cloudflare Worker** konuyu alır, Groq LLM'ye gönderir
2. **Groq API** (`openai/gpt-oss-120b`) 10 sahnelik JSON senaryo üretir
3. Senaryo Telegram'da gösterilir, kullanıcı **✅ Onayla** der
4. **GitHub Actions** (`video-uret.yml`) devreye girer
5. **render.py** Edge-TTS ile ses, Pixabay'dan stok klip, FFmpeg ile kesim/birleştirme yapar
6. Oluşan video Telegram'a gönderilir

## 🏗️ Mimari Akış

```
Telegram Bot (/yeni <konu>)
        │
        ▼
Cloudflare Worker (video-bot-beyin)      ← webhook, Groq istekleri, KV depolama
        │
        ▼
Groq API (openai/gpt-oss-120b)           ← senaryo + sahne listesi üretir
        │
        ▼
Telegram (📋 senaryo + ✅ Onayla butonu)
        │
        ▼
GitHub Actions Dispatch                   ← video-uret.yml tetiklenir
        │
        ▼
Ubuntu Runner → render.py                 ← Edge-TTS + Pixabay + FFmpeg
        │
        ▼
Telegram'a video gönderilir
```

## 📁 Dosya Dağılımı

| Dosya | Sorumluluk |
|-------|------------|
| `worker.js` | Telegram webhook, Groq senaryo üretimi, GitHub dispatch tetikleme |
| `render.py` | Edge-TTS ses sentezi, Pixabay klip bulma, FFmpeg video render, Telegram gönderim |
| `.github/workflows/video-uret.yml` | Render işini çalıştıran GitHub Actions pipeline |
| `.github/workflows/deploy.yml` | Worker'ı Cloudflare Workers'a deploy eden pipeline |
| `wrangler.toml` | Worker konfigürasyonu (name, KV namespace, vars) |

## 🔐 Gerekli Secret'lar

Tüm secret'lar **Cloudflare Workers Settings → Variables → Secrets** alanında tanımlı olmalıdır:

| Secret | Kaynak | Nasıl alınır |
|--------|--------|--------------|
| `TELEGRAM_TOKEN` | @BotFather | https://t.me/BotFather → `/newbot` → token kopyala |
| `GROQ_API_KEY` | Groq Console | https://console.groq.com/keys → yeni key → kopyala |
| `GITHUB_TOKEN` | GitHub | https://github.com/settings/tokens → `repo` scope → kopyala |
| `WEBHOOK_SECRET` | Sen üret | Rastgele uzun string (örn. `SecureWebhook2026!`) |
| `PIXABAY_API_KEY` | Pixabay | https://pixabay.com/api/docs/ → API key → kopyala |

**CI/CD secret'ları (GitHub Actions):**

| Secret | Kaynak | Nerelerde kullanılır? |
|--------|--------|----------------------|
| `TELEGRAM_TOKEN` | @BotFather | render.py video gönderimi |
| `ALLOWED_CHAT_ID` | Telegram | render.py hedef chat |
| `PIXABAY_API_KEY` | Pixabay | render.py klip arama |
| `CLOUDFLARE_API_TOKEN` | Cloudflare | `deploy.yml` — worker deployı |

> ⚠️ **Hiçbir secret'ı `.toml` veya `.env` dosyasına yazma.** `wrangler secret put <İSMİ>` komutuyla Cloudflare'e yük.

## 🚀 Kurulum

### 1. Secret'ları Cloudflare'e yükle

```bash
wrangler secret put TELEGRAM_TOKEN
wrangler secret put GROQ_API_KEY
wrangler secret put GITHUB_TOKEN
wrangler secret put WEBHOOK_SECRET
wrangler secret put PIXABAY_API_KEY
```

Her komut terminalde secret değerini girmeni ister.

### 2. KV Namespace ID'yi ekle

Cloudflare Panel → Workers & Pages → `video-bot-beyin` → **Settings** → **KV Namespaces** → `KV` binding'ine sahip namespace'in **ID'sini** kopyala ve `wrangler.toml`'daki `id` değerine yapıştır:

```toml
[[kv_namespaces]]
binding = "KV"
id = "f15e0459427749a18ca09614530b0807"   # ← kendi ID'nle değiştir
```

### 3. Worker'ı deploy et

```bash
wrangler deploy
```

Çıktıda worker adresin gelecek (örn. `https://video-bot-beyin.<region>.workers.dev`)

### 4. Webhook'u Telegram'a kaydet

```bash
curl "https://api.telegram.org/bot<TELEGRAM_TOKEN>/setWebhook" \
  -d url=https://<worker-adresin>.workers.dev/webhook \
  -d secret_token=<WEBHOOK_SECRET>
```

**Dikkat:** URL kısmında `?secret_token=` DEĞİL, `-d secret_token=` parametresi kullanılır.

### 5. GitHub secret'larını ayarla

GitHub Repo → **Settings** → **Secrets and variables** → **Actions** → şu secret'ları ekle:

- `TELEGRAM_TOKEN`
- `ALLOWED_CHAT_ID`
- `PIXABAY_API_KEY`
- `CLOUDFLARE_API_TOKEN`

## 💬 Kullanım

```bash
# 1. Yeni senaryo iste
/yeni güneş enerjisi

# 2. Bot senaryoyu önerir, ✅ Onayla butonuna tıkla

# 3. Video üretimi başlar (~2-5 dakika), sonra Telegram'da video gelir
```

**Tek kullanıcılı kilit:** Sadece `ALLOWED_USER_ID` olarak tanımlı Telegram hesabı `/yeni` komutunu kullanabilir. Kendi ID'n için `@userinfobot` → `/myid`.

## 🧪 Test Planı

### Hazırlık
```bash
# Worker loglarını canlı izle
wrangler tail video-bot-beyin
```

### Adım 1 — Basit senaryo
```
/yeni güneş panelleri
```
**Beklenen:** "✍️ Konu alındı..." → 5-15 saniye → senaryo + ✅ Onayla

### Adım 2 — Boş konu
```
/yeni
```
**Beklenen:** "Kullanım: /yeni <konu>"

### Adım 3 — Yetkisiz kullanıcı
Başka bir Telegram hesabından `/yeni test` → **"Yetkisiz kullanıcı"** (403)

### Adım 4 — Video üretimi
✅ Onayla'ya tıkla → GitHub Actions → Actions sekmesinde `Video Uretimi` çalışıyor mu bak → video Telegram'da gelir (dikey, Türkçe altyazılı, sesli)

## ⚠️ Bilinen Sınırlamalar

- **Cloudflare Workers ücretsiz planda 10 saniyelik request timeout** — Groq yanıtı bu süreyi geçerse iş başarısız olur (bkz. `GROQ_ATTEMPT_MS`)
- Video boyutu Telegram 50 MB limitine yaklaşırsa otomatik 720p'ye düşer
- Ücretsiz GitHub Actions: 20 dakika/job, 2000 dakikası/ay

## 🛠️ Geliştirme

```bash
# Yerel test (Cloudflare hesabınla)
wrangler dev

# Canlı depolama
wrangler deploy

# Logları izle
wrangler tail video-bot-beyin
```
