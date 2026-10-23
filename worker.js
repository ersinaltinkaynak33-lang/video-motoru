export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    
    if (request.method === "POST" && url.pathname === "/webhook") {
      const body = await request.json();
      const chatId = body.message ? body.message.chat.id : (body.callback_query ? body.callback_query.message.chat.id : null);
      
      // 1. Yeni Konu Gelirse
      if (body.message && body.message.text && body.message.text.startsWith("/yeni")) {
        const topic = body.message.text.replace("/yeni", "").trim();
        
        // Hemen cevap ver
        await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/sendMessage`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat_id: chatId, text: `✍️ Konu alındı: ${topic}\nSenaryo yazılıyor...` })
        });

        try {
          // Güncel ve stabil model (v1)
          const apiUrl = `https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key=${env.GEMINI_KEY}`;
          const geminiRes = await fetch(apiUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ contents: [{ role: "user", parts: [{ text: `Konu: ${topic}. Kısa dikey video senaryosu yaz. SADECE JSON dön: {"baslik":"...", "senaryo":"...", "gorseller":["terim 1", "terim 2"]}` }] }] })
          });
          
          const geminiData = await geminiRes.json();
          
          if (geminiData.error) {
              throw new Error(`API Hatası: ${geminiData.error.message}`);
          }
          
          if(geminiData.candidates) {
              const text = geminiData.candidates[0].content.parts[0].text;
              const data = JSON.parse(text.substring(text.indexOf("{"), text.lastIndexOf("}") + 1));
              
              await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/sendMessage`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  chat_id: chatId,
                  text: `📋 SENARYO:\n\n${data.senaryo}\n\nOnaylıyor musunuz?`,
                  reply_markup: { inline_keyboard: [[{ text: "✅ Onayla", callback_data: `onayla:${JSON.stringify(data)}` }]] }
                })
              });
          } else {
              throw new Error("Geçersiz yanıt formatı.");
          }
        } catch (err) {
          // Hata olursa Telegram'a hatayı yaz
          await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/sendMessage`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ chat_id: chatId, text: `❌ Hata: ${err.message}` })
          });
        }
      }
      
      // 2. Onay Butonuna Basılırsa
      if (body.callback_query) {
        const data = JSON.parse(body.callback_query.data.replace("onayla:", ""));
        const payload = { video_subject: data.baslik, video_script: data.senaryo, video_terms: data.gorseller };
        
        await fetch(`https://api.github.com/repos/${env.GITHUB_USER}/${env.GITHUB_REPO}/actions/workflows/video-uret.yml/dispatches`, {
          method: "POST",
          headers: { "Authorization": `Bearer ${env.GITHUB_TOKEN}`, "Accept": "application/vnd.github.v3+json" },
          body: JSON.stringify({ ref: "main", inputs: { payload: JSON.stringify(payload) } })
        });
        
        await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/answerCallbackQuery`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ callback_query_id: body.callback_query.id, text: "✅ Onaylandı! GitHub videoyu üretmeye başladı." })
        });
      }
      return new Response("OK", { status: 200 });
    }
    return new Response("Bot Aktif", { status: 200 });
  }
};
