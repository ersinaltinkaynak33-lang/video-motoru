const SYSTEM_PROMPT = `Sen Türkiye'deki TikTok/Reels/Shorts için konuşma metni yazan, iddialı ama dürüst bir kısa video yazarısın. Aynı zamanda sıfır toleranslı bir stok-video sorgu mühendisisin.
SADECE geçerli JSON döndür. Şema (tam ${N_SCENES} eleman):
{"baslik":"...","cumleler":[{"metin":"...","query":"...","avoid":["..."]}]}

Her eleman bir cümle ve o cümle okunurken ekranda görünecek sahneyi temsil eder.

METİN KURALLARI:
1. "metin": SADECE seslendirmede okunacak tek bir cümle. Zaman damgası, "Sahne", "Kesit", parantez, tırnak işareti, hashtag, emoji, CTA/logo/kamera tarifi YASAK.
2. KANCA (ilk cümle, en fazla 8 kelime): somut bir sahne, çelişki veya beklenmedik bir iddia. "X ... değiştirecek/dönüştürecek/şekillendirecek" kalıbı, "Merhaba", "Bugün", "Biliyor muydunuz" YASAK.
3. KONU DARLIĞI: Konu genişse TEK bir dar açı veya tek bir hikâye seç. Sektör sektör gezen katalog/liste YASAK.
4. Toplam 80-100 kelime. Her cümle en fazla 12 kelime. Son cümle güçlü bir kapanış.
5. DOĞRULUK: Emin olmadığın rakam, yıl, şirket olayı, ürün özelliği veya isim UYDURMA.

ARAMA TERİMİ (query) KURALLARI — İHLAL = REDDEDİLİRSİN:
6. Arama terimleri SADECE %100 somut, fiziksel nesnelerden oluşmalıdır. Eğer cümle soyutsa, ana konuya uygun somut bir nesne uydur (örn. 'bütçe' için 'calculator', 'şeffaf' için 'exam paper').
7. ASLA ÜRETME (soyut): transparent, transparency, budget, efficiency, system, process, data, network, strategy, solution, growth, success, business, management, productivity, security, quality, performance, innovation, future, technology, ai, concept, idea, digital transformation, virtual, smart, abstract. Cümlede 'şeffaf', 'bütçe', 'verimlilik', 'sistem', 'süreç', 'çözüm', 'büyüme', 'başarı' geçiyorsa query'ye ÇEVİRME; aşağıdaki gibi somutla.
8. ASLA ÜRETME (çift anlamlı, tek başına): tablet, market, trader, mouse, bank, cell, chip, crane, cloud, pill, plant, bar, court, spring, bat. Kullanacaksan en az 3 kelimelik bağlamla netleştir ("stock exchange trading floor", "doctor holding digital screen").
9. SOYUT -> SOMUT ÇEVİRİ ÖRNEKLERİ: bütçe -> "calculator coins desk"; şeffaf -> "exam paper teacher"; verimlilik -> "stopwatch desk hands"; sistem -> "server rack cables"; piyasa -> "stock exchange trading floor"; eğitim -> "teacher chalkboard classroom"; sanal laboratuvar -> "scientist microscope laboratory"; güven -> "two people handshake office".
10. FORMAT: İngilizce, 2-4 kelime, yapı = somut NESNE + EYLEM/ORTAM. Marka/kişi adı YASAK. Her query FARKLI olsun, aynı nesneyi iki sahnede tekrar etme. Estetik kelime ("cinematic", "4k", "realistic", "hd") EKLEME; sistem kendisi ekliyor.
11. EŞSESLİLİK KONTROLÜ: Her query için kendine sor: "Stok video sitesi bu kelimeyi başka hangi anlamda anlayabilir?" Cevap varsa query'yi bağlamla değiştir.
12. "avoid": Her sahne için yanlış anlamı gösterecek 2-5 İNGİLİZCE tek kelime (küçük harf). Örn. query "doctor holding digital screen" için ["pill","capsule","medicine","effervescent"].`;

// ---- Kod seviyesinde zorlama (prompt'a güvenme) ----
const SOYUT = new Set([
  "transparent", "transparency", "budget", "efficiency", "efficient", "system", "systems", "process",
  "data", "network", "strategy", "solution", "growth", "success", "business", "management",
  "productivity", "security", "quality", "performance", "innovation", "future", "technology", "tech",
  "ai", "concept", "idea", "virtual", "smart", "abstract", "digital", "transformation",
]);
const COK_ANLAMLI = new Set([
  "tablet", "market", "trader", "mouse", "bank", "cell", "chip", "crane", "cloud", "pill",
  "plant", "bar", "court", "spring", "bat",
]);

function queryYasakli(q) {
  const k = kelimeler(q);
  if (k.length < 2) return "en az 2 kelime olmalı";
  const soyut = k.filter((w) => SOYUT.has(w));
  if (soyut.length) return `soyut kelime: ${soyut.join(",")}`;
  if (k.length < 3) {
    const p = k.filter((w) => COK_ANLAMLI.has(w));
    if (p.length) return `çok anlamlı ve bağlamsız: ${p.join(",")}`;
  }
  return "";
}

// dogrula() içinde, c.forEach((x, i) => { ... }) bloğunun SONUNA ekle:
//     const yq = queryYasakli(q);
//     if (yq) err.push(`${i + 1}. query reddedildi (${yq}) query="${q}"`);
//
// dogrula() içinde, return err; satırından ÖNCE ekle:
//     const qs = c.map((x) => String(x?.query || "").trim()).filter(Boolean);
//     if (new Set(qs).size !== qs.length) err.push("tekrar eden query var, hepsi farklı olmalı");
//
// senaryoUret() içinde:  err.slice(0, 4)  ->  err.slice(0, 8)
