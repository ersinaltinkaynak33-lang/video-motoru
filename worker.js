export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    
    if (request.method === "POST" && url.pathname === "/webhook") {
      const body = await request.json();
      const chatId = body.message ? body.message.chat.id : (body.callback_query ? body.callback_query.message.chat.id : null);
      
      // 1. Yeni Konu Gelirse (/yeni komutu)
      if (body.message && body.message.text && body.message.text.startsWith("/yeni")) {
        const topic = body.message.text.replace("/yeni", "").trim();
        
        // Kullanıcıya hemen işlemin başladığını bildir
        await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/sendMessage`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat_id: chatId, text: `✍️ Konu alındı: ${topic}\nSenaryo yazılıyor...` })
        });

        try {
          // GROQ API (Llama 3.3 70B) Kullanımı
          const apiUrl = "https://api.groq.com/openai/v1/chat/completions";
          
          const groqRes = await fetch(apiUrl, {
            method: "POST",
            headers: { 
              "Content-Type": "application/json",
              "Authorization": `Bearer ${env.GROQ_API_KEY}` // Cloudflare'deki Groq anahtarını kullanır
            },
            body: JSON.stringify({ 
             model: "llama-3.1-8b-instant", 
              response_format: { "type": "json_object" }, // JSON formatını garantiler
              messages: [
                { role: "system", content: "Sen kısa dikey videolar için senaryo yazan bir yazarsın. SADECE geçerli bir JSON döndür. Başka hiçbir metin yazma." },
                { role: "user", content: `Konu: ${topic}. Kısa dikey video senaryosu yaz. SADECE şu JSON formatında dön: {"baslik":"...", "senaryo":"...", "gorseller":["terim 1", "terim 2"]}` }
              ],
              temperature: 0.7
            })
          });
          
          const groqData = await groqRes.json();
          
          // API'den gelen hatayı kontrol et
          if (groqData.error) {
              throw new Error(groqData.error.message);
          }
          
          if (groqData.choices && groqData.choices[0].message.content) {
              const text = groqData.choices[0].message.content;
              
              // Gelen metni JSON olarak ayrıştır
              const data = JSON.parse(text); 
              
              // Senaryoyu ve onay butonunu gönder
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
              throw new Error("Groq API geçerli bir yanıt vermedi.");
          }
        } catch (err) {
          // Hata olursa doğrudan Telegram'a yaz
          await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/sendMessage`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ chat_id: chatId, text: `❌ Hata: ${err.message}` })
          });
        }
      }
      
      // 2. Onay Butonuna Basılırsa
      if (body.callback_query) {
        try {
          const data = JSON.parse(body.callback_query.data.replace("onayla:", ""));
          const payload = { video_subject: data.baslik, video_script: data.senaryo, video_terms: data.gorseller };
          
          // GitHub Actions'ı tetikle
          await fetch(`https://api.github.com/repos/${env.GITHUB_USER}/${env.GITHUB_REPO}/actions/workflows/video-uret.yml/dispatches`, {
            method: "POST",
            headers: { 
              "Authorization": `Bearer ${env.GITHUB_TOKEN}`, 
              "Accept": "application/vnd.github.v3+json",
              "Content-Type": "application/json"
            },
            body: JSON.stringify({ ref: "main", inputs: { payload: JSON.stringify(payload) } })
          });
          
          // Telegram'a onay bildirimi gönder
          await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/answerCallbackQuery`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ callback_query_id: body.callback_query.id, text: "✅ Onaylandı! GitHub videoyu üretmeye başladı." })
          });
        } catch (err) {
          console.error("Callback Hatası:", err);
        }
      }
      return new Response("OK", { status: 200 });
    }
    return new Response("Bot Aktif", { status: 200 });
  }
};
