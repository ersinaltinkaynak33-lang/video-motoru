async function groqTek(env, messages) {
  // 9‑second timeout for the free Cloudflare Workers plan
  const abortController = new AbortController();
  const timeoutId = setTimeout(() => abortController.abort(), 9000);

  try {
    const response = await fetch("https://api.groq.com/openai/v1/chat/completions", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.GROQ_API_KEY}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: "openai/gpt-oss-120b",
        messages: messages,
        max_tokens: 4000,
        response_format: { type: "json_object" }
      }),
      signal: abortController.signal,
    });

    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(`Groq API error: ${response.status} - ${errorText}`);
    }

    const data = await response.json();

    // Guard against empty / invalid responses from Groq
    if (!data || !data.choices || data.choices.length === 0) {
      throw new Error("Groq returned an empty or invalid response");
    }

    // --- Düzeltilen dönüş mantığı ---
    const raw = data.choices[0].message.content;          // ham metin (string)
    const clean = raw
      .replace(/```json/gi, "")                           // başlangıç markdown
      .replace(/```/g, "")                                // bitiş markdown
      .trim();                                            // boşlukları kes
    return JSON.parse(clean);                             // parsed object
    // ---------------------------------
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("Groq API request timed out after 9 seconds");
    }
    throw error;   // re‑throw any other error (e.g., network, auth)
  } finally {
    // Always clear the timeout to avoid leaks
    clearTimeout(timeoutId);
  }
}