const tg = (env, method, payload) =>
  fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

// ACIMASIZ PROMPT: Görsellerin saçmalamasını (Buda heykeli vb.) engelleyen katı kurallar
const SYSTEM_PROMPT = `Sen Türkiye'deki TikTok/Reels/Shorts için konuşma metni yazan acımasız bir kısa video yazarısın.
SADECE geçerli JSON döndür. Şema:
{"baslik":"...","senaryo":"...","gorseller":["english query"]}

KURALLAR:
1. "senaryo" SADECE seslendirmede okunacak cümleleri içerir. Zaman damgası, "Sahne", "Kesit", "Ses:", "Metin:", parantez, tırnak işareti, hashtag, kanal/logo/CTA talimatı YASAK. Kamera veya görüntü tarifi YASAK.
2. İlk cümle (Kanca) en fazla 8 kelime olsun: ilk saniyede merak, çelişki veya somut bir iddia. "Merhaba", "Bugün", "Biliyor muydunuz" ve "dünyayı değiştiren" gibi girişler KESİNLİKLE YASAK.
3. Toplam 85-105 kelime. Her cümle en fazla 12 kelime. Cümle uzunluklarını değiştir.
4. Her 2 cümlede en az bir somut unsur olsun: sayı, isim, yer, tarih veya gündelik bir an. Şu kalıplar YASAK: "geleceğin", "sınırları zorlamak", "hayatımızın bir parçası", "yolculuğa katıl", "çağında".
5. İkinci tekil şahısla konuş ("sen"). En az bir net görüş cümlesi olsun. Uydurma istatistik YASAK.
6. Son cümle bir sonraki videoya merak bıraksın veya ilk cümleye geri dönsün. "Takip et", "katıl" gibi boş çağrı YASAK.
7. "gorseller": tam 10 adet, İNGİLİZCE, 2 kelime. KESİNLİKLE SOMUT NESNELER VE EYLEMLER kullan (örn. "person typing laptop", "robot arm factory", "glowing microchip", "drone flying city"). Soyut kelime (future, technology, innovation, concept, ai, cross, god, religion, flag) YASAK. Görseller senaryo sırasını izlesin.`;

async function yeniSenaryo(env, chatId, topic) {
  try {
    const res = await fetch("https://api.groq.com/openai/v1/chat/completions", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${env.GROQ_API_KEY}`,
      },
      body: JSON.stringify({
        model: "openai/gpt-oss-120b",
        temperature: 0.7,
        messages: [
          { role: "system", content: SYSTEM_PROMPT },
          { role: "user", content: `Konu: ${topic}` },
        ],
      }),
      signal: AbortSignal.timeout(15000),
    });
    
    const j = await res.json();
    if (j.error) throw new Error(j.error.message);
    
    let text = j.choices[0].message.content;
    text = text.replace(/```json/g, "").replace(/```/g, "").trim();
    const data = JSON.parse(text);

    const id = crypto.randomUUID().slice(0, 8);
    await env.KV.put(`s:${id}`, JSON.stringify(data), { expirationTtl: 3600 });

    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: `📋 SENARYO:\n\n${data.senaryo}\n\nOnaylıyor musunuz?`,
      reply_markup: { inline_keyboard: [[{ text: "✅ Onayla", callback_data: `onayla:${id}` }]] },
    });
  } catch (err) {
    await tg(env, "sendMessage", { chat_id: chatId, text: `❌ Hata: ${err.message}` });
  }
}

async function onayla(env, cq) {
  const id = cq.data.replace("onayla:", "");
  const raw = await env.KV.get(`s:${id}`);
  
  if (!raw) {
    return tg(env, "answerCallbackQuery", { callback_query_id: cq.id, text: "⏳ Süresi dolmuş, tekrar /yeni deneyin." });
  }

  await env.KV.delete(`s:${id}`);

  const d = JSON.parse(raw);
  const payload = { video_subject: d.baslik, video_script: d.senaryo, video_terms: d.gorseller };

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

  await tg(env, "answerCallbackQuery", {
    callback_query_id: cq.id,
    text: gh.status === 204 ? "✅ Onaylandı! Video üretimi başladı." : `❌ GitHub hatası: ${gh.status}`,
  });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/webhook")
      return new Response("Bot Aktif");

    const body = await request.json();
    const chatId = body.message?.chat.id ?? body.callback_query?.message.chat.id;
    const fromId = body.message?.from?.id ?? body.callback_query?.from?.id;

    if (String(fromId) !== env.ALLOWED_USER_ID) {
        return new Response("Yetkisiz kullanıcı", { status: 403 });
    }

    const m = body.message?.text?.match(/^\/yeni(?:@\w+)?\s*(.*)$/s);
    if (m) {
      const topic = m[1].trim();
      if (!topic) {
        ctx.waitUntil(tg(env, "sendMessage", { chat_id: chatId, text: "Kullanım: /yeni <konu>" }));
      } else {
        ctx.waitUntil((async () => {
          await tg(env, "sendMessage", { chat_id: chatId, text: `✍️ Konu alındı: ${topic}\nSenaryo yazılıyor...` });
          await yeniSenaryo(env, chatId, topic);
        })());
      }
    } else if (body.callback_query?.data?.startsWith("onayla:")) {
      ctx.waitUntil(onayla(env, body.callback_query));
    }

    return new Response("OK");
  },
};
