// Cloudflare Worker: Telegram -> Groq (senaryo + sahne) -> GitHub Actions dispatch
//
// Gerekli secret'lar (wrangler secret put ...):
//   TELEGRAM_TOKEN, GROQ_API_KEY, GITHUB_TOKEN, WEBHOOK_SECRET
// Webhook'u şu şekilde kaydedin (secret_token ŞART, yoksa herkes sahte update gönderebilir):
//   curl "https://api.telegram.org/bot<TOKEN>/setWebhook" \\
//        -d url=https://<worker-adresi>/webhook -d secret_token=<WEBHOOK_SECRET>

const N_SCENES = 10;
const GROQ_ATTEMPT_MS = 6000; // Her deneme max 6 sn — Cloudflare ücretsiz plan 10sn request timeout'unun altında güvenli marj (2 deneme = ~12sn toplam)

const tg = (env, method, payload) =>
  fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).catch((e) => console.error("Telegram hatası:", method, e.message));

const SYSTEM_PROMPT = `Sen Türkiye'deki TikTok/Reels/Shorts için konuşma metni yazan, iddialı ama dürüst bir kısa video yazarısın. Aynı zamanda sıfır toleranslı bir stok-video sorgu mühendisisin.
SADECE geçerli JSON döndür. Başka hiçbir metin, açıklama ya da markdown kodu yazma. Şema (tam ${N_SCENES} eleman):
{"baslik":"...","cumleler":[{"metin":"...","query":"...","alt":"...","avoid":["..."]}]}

Her eleman bir cümle ve o cümle okunurken ekranda görünecek sahneyi temsil eder.

METİN KURALLARI:
1. "metin": SADECE seslendirmede okunacak tek bir cümle. Zaman damgası, "Sahne", "Kesit", parantez, tırnak işareti, hashtag, emoji, CTA/logo/kamera tarifi YASAK.
2. KANCA (ilk cümle, en fazla 8 kelime): somut bir sahne, çelişki veya beklenmedik ama DOĞRU bir iddia. YASAK klişeler: "devrim yaratacak", "çağ", "gelecek geldi", "hayatımızı değiştirecek", "X ... değiştirecek/dönüştürecek/şekillendirecek", "Merhaba", "Bugün", "Biliyor muydunuz". İyi kanca kalıpları: gündelik bir nesnenin beklenmedik davranışı ("Süpürgeniz odanızın planını çoktan çıkardı."), bir çelişki, somut bir sahne.
3. KONU DARLIĞI: Konu genişse TEK bir dar açı veya tek bir hikâye seç. Sektör sektör gezen katalog/liste YASAK.
4. Toplam 80-100 kelime. Her cümle en fazla 12 kelime. Son cümle güçlü bir kapanış.
5. DOĞRULUK: Emin olmadığın rakam, yıl, şirket olayı, ürün özelliği veya isim UYDURMA.

ARAMA TERİMİ (query) KURALLARI — İHLAL = REDDEDİLİRSİN:
6. Arama terimleri SADECE %100 somut, fiziksel nesnelerden oluşmalıdır. Eğer cümle soyutsa, ana konuya uygun somut bir nesne uydur (örn. 'bütçe' için 'calculator', 'şeffaf' için 'exam paper'). Bu örnekler yalnızca nesneyi gösterir; query yine 2-4 kelime olmalıdır (calculator -> "calculator coins desk").
7. ASLA ÜRETME (soyut): transparent, transparency, budget, efficiency, system, process, data, network, strategy, solution, growth, success, business, management, productivity, security, quality, performance, innovation, future, technology, ai, concept, idea, digital, transformation, virtual, smart, abstract. Cümlede 'şeffaf', 'bütçe', 'verimlilik', 'sistem', 'süreç', 'çözüm', 'büyüme', 'başarı' geçiyorsa query'ye ÇEVİRME; aşağıdaki gibi somutla.
8. ASLA ÜRETME (çift anlamlı, tek başına): tablet, market, trader, mouse, bank, cell, chip, crane, cloud, pill, plant, bar, court, spring, bat. Kullanacaksan en az 3 kelimelik bağlamla netleştir ("stock exchange trading floor", "doctor reading xray film").
9. SOYUT -> SOMUT ÇEVİRİ ÖRNEKLERİ: bütçe -> "calculator coins desk"; şeffaf -> "exam paper teacher"; verimlilik -> "stopwatch desk hands"; sistem -> "server rack cables"; piyasa -> "stock exchange trading floor"; eğitim -> "teacher chalkboard classroom"; sanal laboratuvar -> "scientist microscope laboratory"; güven -> "two people handshake office".
10. FORMAT: İngilizce, 2-4 kelime, yapı = somut NESNE + EYLEM/ORTAM. Marka/kişi adı YASAK. Her query FARKLI olsun, aynı nesneyi iki sahnede tekrar etme. Estetik kelime ("cinematic", "4k", "realistic", "hd") EKLEME; sistem kendisi ekliyor.
11. EŞSESLİLİK KONTROLÜ: Her query için kendine sor: "Stok video sitesi bu kelimeyi başka hangi anlamda anlayabilir?" Cevap varsa query'yi bağlamla değiştir.
12. "avoid": Her sahne için yanlış anlamı gösterecek 2-5 İNGİLİZCE tek kelime (küçük harf). Örn. query "doctor reading xray film" için ["pill","capsule","medicine","effervescent"].
13. "alt": query bulunamazsa kullanılacak İKİNCİ somut sorgu. Aynı kurallar (İngilizce, 2-4 kelime, somut nesne+ortam), ama query'den FARKLI bir nesne veya açı olmalı. Hiçbir query/alt başka sahnedekiyle aynı olamaz.
14. TEMA KİLİDİ: Konu doğa, hayvan, tarım, enerji veya seyahat DEĞİLSE; doğa, manzara, okyanus, deniz, plaj, orman, dağ, göl, nehir, gökyüzü, bulut, gün batımı, ateş, şömine, kar, sis, duman, rüzgar türbini, hayvan çağrıştıran HİÇBİR arama terimi kullanma. Konu ev/teknoloji/iş ise sadece o ortamın nesnelerini yaz (örn. "robot vacuum living room", "washing machine buttons", "phone screen hand", "wall socket plug").`;

// ---- Kod seviyesinde zorlama (prompt'a güvenme) ----
const SOYUT = new Set([
  "transparent", "transparency", "budget", "efficiency", "efficient", "system", "systems", "process",
  "data", "network", "strategy", "solution", "growth", "success", "business", "management",
  "productivity", "security", "quality", "performance", "innovation", "future", "technology", "tech",
  "ai", "concept", "idea", "virtual", "smart", "abstract", "transformation",
]);
const COK_ANLAMLI = new Set([
  "tablet", "market", "trader", "mouse", "bank", "cell", "chip", "crane", "cloud", "pill",
  "plant", "bar", "court", "spring", "bat",
]);
const ESTETIK = new Set(["cinematic", "realistic", "4k", "hd", "uhd", "8k", "1080p"]);
const DOGA = new Set([
  "nature", "landscape", "scenery", "ocean", "sea", "beach", "shore", "coast", "waves", "forest", "woods",
  "tree", "trees", "mountain", "mountains", "hill", "hills", "valley", "lake", "river", "waterfall",
  "sky", "clouds", "sunset", "sunrise", "fire", "fireplace", "flame", "flames", "snow", "fog", "mist",
  "smoke", "wind", "turbine", "windmill", "wildlife", "animal", "animals", "bird", "birds", "fox", "deer",
]);
const DOGA_KONU = /doğa|orman|deniz|okyanus|dağ|hayvan|manzara|gökyüz|tarım|çiftlik|iklim|çevre|bitki|kuş|balık|seyahat|gezi|tatil|köy|enerji|rüzgar|güneş|yangın|ateş/i;

const kelimeler = (v) =>
  String(v ?? "")
    .toLowerCase()
    .replace(/['’]/g, "")
    .replace(/[^a-z0-9\\s]/g, " ")
    .split(/\\s+/)
    .filter(Boolean);

function queryYasakli(q, dogaYasak = false) {
  const k = kelimeler(q);
  if (k.length < 2) return "en az 2 kelime olmalı";
  const soyut = k.filter((w) => SOYUT.has(w));
  if (soyut.length) return `soyut kelime: ${soyut.join(",")}`;
  if (dogaYasak) {
    const d = k.filter((w) => DOGA.has(w));
    if (d.length) return `doğa/manzara kelimesi (tema kilidi): ${d.join(",")}`;
  }
  if (k.length < 3) {
    const p = k.filter((w) => COK_ANLAMLI.has(w));
    if (p.length) return `çok anlamlı ve bağlamsız: ${p.join(",")}`;
  }
  return "";
}

function temizle(d) {
  if (!Array.isArray(d?.cumleler)) return d;
  for (const x of d.cumleler) {
    if (!x || typeof x !== "object") continue;
    for (const alan of ["query", "alt"]) {
      x[alan] = kelimeler(x[alan])
        .filter((w) => !ESTETIK.has(w))
        .slice(0, 4)
        .join(" ");
    }
    const ham = Array.isArray(x.avoid) ? x.avoid : typeof x.avoid === "string" ? [x.avoid] : [];
    x.avoid = [...new Set(ham.flatMap(kelimeler))].slice(0, 5);
  }
  return d;
}

function dogrula(d, topic = "") {
  const err = [];
  const dogaYasak = !DOGA_KONU.test(String(topic));
  if (!d || typeof d.baslik !== "string" || !d.baslik.trim()) err.push("baslik eksik");
  const c = Array.isArray(d?.cumleler) ? d.cumleler : [];
  if (c.length !== N_SCENES) err.push(`cumleler tam ${N_SCENES} eleman olmalı, ${c.length} geldi`);
  c.forEach((x, i) => {
    const m = typeof x?.metin === "string" ? x.metin.trim() : "";
    const q = typeof x?.query === "string" ? x.query.trim() : "";
    if (!m) err.push(`${i + 1}. metin boş`);
    if (/[()\[\]\""“”#]|sahne\s*\d|kesit/i.test(m)) err.push(`${i + 1}. metinde yasak karakter/ifade var`);
    const a = typeof x?.alt === "string" ? x.alt.trim() : "";
    if (!q) err.push(`${i + 1}. query boş`);
    else {
      const yq = queryYasakli(q, dogaYasak);
      if (yq) err.push(`${i + 1}. query reddedildi (${yq}) query="${q}"`);
    }
    if (!a) err.push(`${i + 1}. alt boş`);
    else {
      const ya = queryYasakli(a, dogaYasak);
      if (ya) err.push(`${i + 1}. alt reddedildi (${ya}) alt="${a}"`);
      if (a === q) err.push(`${i + 1}. alt, query ile aynı`);
    }
    if (!Array.isArray(x?.avoid) || x.avoid.length < 1) err.push(`${i + 1}. avoid boş (query="${q}")`);
  });
  const qs = c.flatMap((x) => [String(x?.query || "").trim(), String(x?.alt || "").trim()]).filter(Boolean);
  if (new Set(qs).size !== qs.length) err.push("tekrar eden query/alt var, hepsi farklı olmalı");
  const wc = c.map((x) => String(x?.metin || "").trim().split(/\\s+/).filter(Boolean).length);
  const toplam = wc.reduce((a, b) => a + b, 0);
  if (c.length && (toplam < 65 || toplam > 115)) err.push(`toplam ${toplam} kelime (80-100 olmalı)`);
  if (wc[0] > 10) err.push("ilk cümle 8 kelimeyi aşıyor");
  if (/(değiştirecek|dönüştürecek|şekillendirecek|şekillenecek|devrim|yeni çağ|çağı|geleceği|gelecek geldi|biliyor muydunuz)/i.test(String(c[0]?.metin || "")))
    err.push("kanca klişe/ansiklopedi kalıbında (devrim/çağ/değiştirecek/dönüştürecek/biliyor muydunuz)");
  return err;
}

async function groqTek(env, messages) {
  const res = await fetch("https://api.groq.com/openai/v1/chat/completions", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${env.GROQ_API_KEY}` },
    body: JSON.stringify({
      model: "openai/gpt-oss-120b",
      temperature: 0.7,
      max_tokens: 4000,
      messages,
    }),
    signal: AbortSignal.timeout(GROQ_ATTEMPT_MS),
  });
  if (!res.ok) {
    const errText = await res.text();
    throw new Error(`Groq HTTP ${res.status}: ${errText.slice(0, 200)}`);
  }
  const j = await res.json();
  if (j.error) throw new Error(j.error.message);
  let text = j.choices?.[0]?.message?.content;
  if (!text || !text.trim()) {
    console.log("Groq boş yanıt. Finish reason:", j.choices?.[0]?.finish_reason);
    throw new Error(`Groq boş yanıt döndürdü (Finish reason: ${j.choices?.[0]?.finish_reason || 'bilinmiyor'})`);
  }
  text = text.replace(/```json/gi, "").replace(/```/g, "").trim();
  return JSON.parse(text);
}

async function senaryoUret(env, topic) {
  const messages = [
    { role: "system", content: SYSTEM_PROMPT },
    { role: "user", content: `Konu: ${topic}` },
  ];
  let sonHata = "";
  for (let deneme = 0; deneme < 2; deneme++) {
    try {
      const d = temizle(await groqTek(env, messages));
      const err = dogrula(d, topic);
      if (!err.length) return d;
      sonHata = err.slice(0, 8).join("; ");
      console.log("Doğrulama reddi:", sonHata);
      messages.push({ role: "assistant", content: JSON.stringify(d) });
      messages.push({ role: "user", content: `Reddedildi: ${sonHata}. Kuralları düzelterek JSON'u baştan yaz.` });
    } catch (e) {
      sonHata = e.message;
    }
  }
  throw new Error(`Senaryo üretilemedi (${sonHata})`);
}

async function yeniSenaryo(env, chatId, topic) {
  try {
    const d = await senaryoUret(env, topic);
    const scenes = d.cumleler.map((x) => ({ text: x.metin.trim(), query: x.query.trim(), alt: x.alt.trim(), avoid: x.avoid }));
    const id = crypto.randomUUID().slice(0, 8);
    await env.KV.put(`s:${id}`, JSON.stringify({ baslik: d.baslik.trim(), scenes }), { expirationTtl: 3600 });

    const senaryo = scenes.map((s) => s.text).join(" ");
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: `📋 ${d.baslik.trim()}\n\n${senaryo}\n\n🎞 ${scenes.map((s) => s.query).join(" · ")}\n\nOnaylıyor musunuz?`,
      reply_markup: { inline_keyboard: [[{ text: "✅ Onayla", callback_data: `onayla:${id}` }]] },
    });
  } catch (err) {
    console.error("Senaryo hatası:", err);
    await tg(env, "sendMessage", { chat_id: chatId, text: `❌ ${err.message}` });
  }
}

async function onayla(env, cq) {
  const answer = (text) => tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text });
  const id = cq.data.replace("onayla:", "");
  const key = `s:${id}`;
  const raw = await env.KV.get(key);
  if (!raw) return answer("⏳ Süresi dolmuş veya zaten onaylanmış. /yeni ile tekrar deneyin.");

  await env.KV.delete(key);
  const d = JSON.parse(raw);
  const payload = { video_subject: d.baslik, scenes: d.scenes };

  const gh = await fetch(
    `https://api.github.com/repos/${env.GITHUB_USER}/${env.GITHUB_REPO}/actions/workflows/video-uret.yml/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "telegram-video-bot",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs: { payload: JSON.stringify(payload) } }),
    }
  );

  if (gh.status === 204) {
    if (cq.message) {
      await tg(env, "editMessageReplyMarkup", {
        chat_id: cq.message.chat.id,
        message_id: cq.message.message_id,
        reply_markup: { inline_keyboard: [] },
      });
    }
    return answer("✅ Onaylandı! Video üretimi başladı.");
  }
  await env.KV.put(key, raw, { expirationTtl: 3600 });
  const detay = (await gh.text()).slice(0, 150);
  return answer(`❌ GitHub ${gh.status}: ${detay}`.slice(0, 190));
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/webhook") return new Response("Bot Aktif");

    if (!env.WEBHOOK_SECRET || request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("Yetkisiz", { status: 401 });
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return new Response("Geçersiz JSON", { status: 400 });
    }

    const msg = body.message;
    const cq = body.callback_query;
    const chatId = msg?.chat?.id ?? cq?.message?.chat?.id;
    const fromId = msg?.from?.id ?? cq?.from?.id;
    if (String(fromId) !== String(env.ALLOWED_USER_ID)) return new Response("Yetkisiz kullanıcı", { status: 403 });

    const m = msg?.text?.match(/^\/yeni(?:@\w+)?\s*(.*)$/s);
    if (m) {
      const topic = m[1].trim().slice(0, 300);
      if (!topic) {
        ctx.waitUntil(tg(env, "sendMessage", { chat_id: chatId, text: "Kullanım: /yeni <konu>" }));
      } else {
        ctx.waitUntil(
          (async () => {
            await tg(env, "sendMessage", { chat_id: chatId, text: `✍️ Konu alındı: ${topic}\nSenaryo yazılıyor...` });
            await yeniSenaryo(env, chatId, topic);
          })()
        );
      }
    } else if (cq?.data?.startsWith("onayla:")) {
      ctx.waitUntil(onayla(env, cq));
    }
    return new Response("OK");
  },
};