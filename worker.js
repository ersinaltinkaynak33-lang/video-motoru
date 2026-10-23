// Cloudflare Worker: Telegram -> Groq (senaryo + sahne) -> GitHub Actions dispatch
//
// Gerekli secret'lar (wrangler secret put ...):
//   TELEGRAM_TOKEN, GROQ_API_KEY, GITHUB_TOKEN, WEBHOOK_SECRET
// Webhook'u şu şekilde kaydedin (secret_token ŞART, yoksa herkes sahte update gönderebilir):
//   curl "https://api.telegram.org/bot<TOKEN>/setWebhook" \
//        -d url=https://<worker-adresi>/webhook -d secret_token=<WEBHOOK_SECRET>

const N_SCENES = 10;
const GROQ_ATTEMPT_MS = 11000; // 2 deneme + Telegram çağrıları < 30 sn (ctx.waitUntil sınırı)

const tg = (env, method, payload) =>
  fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).catch((e) => console.error("Telegram hatası:", method, e.message));

const SYSTEM_PROMPT = `Sen Türkiye'deki TikTok/Reels/Shorts için konuşma metni yazan, iddialı ama dürüst bir kısa video yazarısın.
SADECE geçerli JSON döndür. Şema (tam ${N_SCENES} eleman):
{"baslik":"...","cumleler":[{"metin":"...","query":"...","avoid":["..."]}]}

Her eleman bir cümle ve o cümle okunurken ekranda görünecek sahneyi temsil eder.

KURALLAR:
1. "metin": SADECE seslendirmede okunacak tek bir cümle. Zaman damgası, "Sahne", "Kesit", parantez, tırnak işareti, hashtag, emoji, CTA/logo/kamera tarifi YASAK.
2. KANCA (ilk cümle, en fazla 8 kelime): somut bir sahne, çelişki veya beklenmedik bir iddia olmalı. "X ... değiştirecek/dönüştürecek/şekillendirecek" gibi ansiklopedi girişleri, "Merhaba", "Bugün", "Biliyor muydunuz" YASAK.
3. KONU DARLIĞI: Konu geniş ise (yapay zekâ, gelecek, teknoloji) TEK bir dar açı veya tek bir hikâye seç ve onu anlat. Sektör sektör gezen katalog/liste YASAK; videonun baştan sona tek bir ana fikri olsun.
4. Toplam 80-100 kelime. Her cümle en fazla 12 kelime, uzunlukları değiştir. Son cümle güçlü bir kapanış olsun.
5. DOĞRULUK: Emin olmadığın hiçbir rakam, yıl, şirket olayı, ürün özelliği veya isim UYDURMA. Şüphen varsa rakam verme.
6. "query": İngilizce, 2-4 kelime, stok video sitesinde aranacak, kameraya çekilebilir SOMUT nesne+eylem+ortam. Marka/kişi adı ve soyut kelime (future, technology, ai, concept, innovation) YASAK. Her query farklı olsun.
7. EŞSESLİLİK KONTROLÜ: Her query için kendine sor: "Stok video sitesi bu kelimeyi başka hangi anlamda anlayabilir?" Birden fazla anlamı olan kelimeyi (örn. tablet=hap, market=pazar yeri, trader=sokak satıcısı, mouse=fare hayvanı, bank=nehir kıyısı, cell=hapishane hücresi, chip=cips, crane=turna kuşu, cloud=bulut) ya bağlam kelimesiyle netleştir ("doctor digital screen", "stock exchange trading floor") ya da hiç kullanma.
8. "avoid": Her sahne için, yanlış anlamı gösterecek 2-5 İNGİLİZCE tek kelime (küçük harf). Örn. query "doctor digital screen" için ["pill","capsule","medicine","effervescent"]. Bu kelimeler etiketlerinde geçen videolar elenecek.`;

function dogrula(d) {
  const err = [];
  if (!d || typeof d.baslik !== "string" || !d.baslik.trim()) err.push("baslik eksik");
  const c = Array.isArray(d?.cumleler) ? d.cumleler : [];
  if (c.length !== N_SCENES) err.push(`cumleler tam ${N_SCENES} eleman olmalı, ${c.length} geldi`);
  c.forEach((x, i) => {
    const m = typeof x?.metin === "string" ? x.metin.trim() : "";
    const q = typeof x?.query === "string" ? x.query.trim() : "";
    if (!m) err.push(`${i + 1}. metin boş`);
    if (/[()\[\]"“”#]|sahne\s*\d|kesit/i.test(m)) err.push(`${i + 1}. metinde yasak karakter/ifade var`);
    if (!/^[A-Za-z]+( [A-Za-z]+){1,3}$/.test(q)) err.push(`${i + 1}. query İngilizce 2-4 kelime olmalı`);
    const av = x?.avoid;
    if (!Array.isArray(av) || av.length < 2 || av.length > 6 || !av.every((w) => typeof w === "string" && /^[a-z]+$/.test(w)))
      err.push(`${i + 1}. avoid 2-5 küçük harfli İngilizce kelime olmalı`);
  });
  const wc = c.map((x) => String(x?.metin || "").trim().split(/\s+/).filter(Boolean).length);
  const toplam = wc.reduce((a, b) => a + b, 0);
  if (c.length && (toplam < 65 || toplam > 115)) err.push(`toplam ${toplam} kelime (80-100 olmalı)`);
  if (wc[0] > 10) err.push("ilk cümle 8 kelimeyi aşıyor");
  if (/(değiştirecek|dönüştürecek|şekillendirecek|şekillenecek)/i.test(String(c[0]?.metin || "")))
    err.push("kanca ansiklopedi kalıbında (değiştirecek/dönüştürecek)");
  return err;
}

async function groqTek(env, messages) {
  const res = await fetch("https://api.groq.com/openai/v1/chat/completions", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${env.GROQ_API_KEY}` },
    body: JSON.stringify({
      model: "openai/gpt-oss-120b",
      temperature: 0.7,
      reasoning_effort: "low",
      max_completion_tokens: 3000,
      response_format: { type: "json_object" },
      messages,
    }),
    signal: AbortSignal.timeout(GROQ_ATTEMPT_MS),
  });
  if (!res.ok) throw new Error(`Groq HTTP ${res.status}`);
  const j = await res.json();
  if (j.error) throw new Error(j.error.message);
  let text = j.choices?.[0]?.message?.content;
  if (!text || !text.trim()) throw new Error("Groq boş yanıt döndürdü");
  text = text.replace(/```json/gi, "").replace(/```/g, "").trim();
  return JSON.parse(text);
}

// En fazla 2 deneme; ikincide ilk denemenin hataları modele geri bildirim olarak verilir.
async function senaryoUret(env, topic) {
  const messages = [
    { role: "system", content: SYSTEM_PROMPT },
    { role: "user", content: `Konu: ${topic}` },
  ];
  let sonHata = "";
  for (let deneme = 0; deneme < 2; deneme++) {
    try {
      const d = await groqTek(env, messages);
      const err = dogrula(d);
      if (!err.length) return d;
      sonHata = err.slice(0, 4).join("; ");
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
    const scenes = d.cumleler.map((x) => ({ text: x.metin.trim(), query: x.query.trim(), avoid: x.avoid }));
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
    // çift tıklamayı engelle: butonu kaldır
    if (cq.message) {
      await tg(env, "editMessageReplyMarkup", {
        chat_id: cq.message.chat.id,
        message_id: cq.message.message_id,
        reply_markup: { inline_keyboard: [] },
      });
    }
    return answer("✅ Onaylandı! Video üretimi başladı.");
  }
  // başarısızsa senaryo kaybolmasın
  await env.KV.put(key, raw, { expirationTtl: 3600 });
  const detay = (await gh.text()).slice(0, 150);
  return answer(`❌ GitHub ${gh.status}: ${detay}`.slice(0, 190));
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/webhook") return new Response("Bot Aktif");

    // Telegram update'leri imzasız gelir; from.id kolayca taklit edilir. Gizli başlık şart.
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
