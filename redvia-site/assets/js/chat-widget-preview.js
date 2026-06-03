/* ============================================================
   Redvia Assistant — FRONTEND PREVIEW (throwaway)
   - Starter chips  -> PRE-WRITTEN canned answers (no AI), unless chip.llm
   - Custom typing  -> LLM via local proxy /api/chat (short, grounded)
   Auto-behavior by page type:
   - Product pages + match page -> full panel auto-opens
   - Everything else            -> gentle teaser (per page load)
   ============================================================ */
(function () {
  "use strict";

  var CONTACT_LINE =
    'You can also reach our team directly at ' +
    '<a href="mailto:trade@redvia-bry.com">trade@redvia-bry.com</a> ' +
    'or <a href="tel:+16513991568">+1 (651) 399-1568</a>.';

  var BASE_API = ""; // same origin (proxy serves site + /api/chat)

  // Product name from the page itself (works for any product page).
  var PROD_EL = document.querySelector(".prod-hero__name");
  var PRODUCT_NAME = PROD_EL ? PROD_EL.textContent.trim() : "";

  // -- Page-type detection ------------------------------------------
  var PATH = location.pathname;
  var IS_PRODUCT = /\/ingredients\/[^/]+\.html$/.test(PATH); // individual product, not the listing
  var IS_MATCH = /match\.html$/.test(PATH);
  var IS_BLACK_GOJI = /black-goji-berry/.test(PATH);
  var IS_HOME = /index\.html$/.test(PATH) || PATH === "/" || PATH === "";

  function detectContext() {
    if (IS_BLACK_GOJI) return "black-goji";
    if (IS_PRODUCT) return "product";
    if (IS_MATCH) return "match";
    if (IS_HOME) return "home";
    return "generic";
  }
  var CTX = detectContext();

  // Full panel auto-opens on product pages only. The match page keeps the
  // quiz as the main path, so the bot stays a teaser there (no auto-open).
  var FULL_OPEN = IS_PRODUCT;

  // Session-level throttle so the panel doesn't re-pop on every product page.
  // sessionStorage = per-visit (resets when the tab/browser session ends).
  var STORAGE_OK = (function () {
    try { sessionStorage.setItem("rv_ss_test", "1"); sessionStorage.removeItem("rv_ss_test"); return true; }
    catch (e) { return false; }
  })();
  var SS_AUTO = "rvBotAutoOpened";    // already auto-opened once this session
  var SS_DISMISS = "rvBotDismissed";  // visitor manually closed the panel this session

  // -- Shared chip sets ---------------------------------------------
  var CHIP_CERTS = {
    q: "What certifications do you have?",
    a: "<p>The plantation holds <strong>EU BCS Organic, USDA NOP, JAS Organic, BRCGS Food Safety,</strong> and <strong>CNAS</strong> laboratory accreditation. Scans are on every product page.</p>",
    defer: "Need a specific certificate for your market? Our team can send the current copy."
  };
  var CHIP_SPEC = {
    q: "Send me a spec sheet",
    a: "<p>Happy to get that moving — specs, CoAs, and samples typically go out within 48 hours once our team has your details.</p>",
    offerLead: true
  };

  // -- Per-context content ------------------------------------------
  var CONTEXTS = {
    home: {
      teaserLabel: "Redvia Assistant",
      teaser: "New here? Tell me what you're formulating — I'll point you to the right goji format.",
      greeting:
        "<p>Hi — I'm the Redvia sourcing assistant. I can help you find the right goji format, talk through applications, or pass a brief to our team.</p>" +
        "<p>" + CONTACT_LINE + "</p><p>What are you working on?</p>",
      chips: [
        { q: "Red vs. black goji?",
          a: "<p><strong>Red goji</strong> (Ningxia / Qinghai) is the classic — naturally sweet and versatile across tea, snack, topping, and beverage lines.</p>" +
             "<p><strong>Black goji</strong> is rarer and prized for its anthocyanin and polyphenol content — for antioxidant, beauty, and premium positioning.</p>" },
        CHIP_CERTS,
        { q: "Which format for a beverage?",
          a: "<p>For beverages, the strong fits are <strong>Red Goji Puree</strong> (real fruit color and body, soluble cold) and <strong>Goji Polysaccharide Powder</strong> when you want a functional claim without changing mouthfeel.</p>",
          offerLead: true },
        { q: "I need whole dried berries",
          a: "<p><strong>Ningxia Red</strong> gives the most recognizable berry presence; <strong>Black Goji</strong> reads as a darker premium counterpoint. Size grades run 180–380 berries per 50g.</p>",
          offerLead: true }
      ]
    },
    "black-goji": {
      teaserLabel: "Ask about Black Goji",
      teaser: "Curious about black goji? Ask me about applications, formats, or specs.",
      greeting:
        "<p>You're looking at <strong>Black Goji Berry</strong> — our high-anthocyanin format for antioxidant, beauty, and premium wellness positioning.</p>" +
        "<p>" + CONTACT_LINE + "</p><p>Ask me anything, or tell me what you're building.</p>",
      chips: [
        { q: "What makes black goji different?",
          a: "<p>Black goji is rich in <strong>anthocyanins and polyphenols</strong> — that's what sets it apart from red goji. The deep purple-black color and antioxidant story make it a premium, beauty-oriented format.</p>" },
        { q: "Best applications?",
          a: "<p>It works best in <strong>premium dried-berry formats, functional tea, beauty nutrition, antioxidant blends,</strong> and <strong>high-end wellness lines</strong>.</p>" },
        { q: "Anthocyanin content?",
          a: "<p>Black goji is valued specifically for its anthocyanin and polyphenol density — that's its whole reason for being a separate category.</p>",
          defer: "Exact assay values vary by lot, so I won't quote a number off the cuff — our team can send the current CoA with measured figures." },
        CHIP_SPEC
      ]
    },
    // Generic product page: name pulled from the DOM; product-specific
    // questions route to the LLM (we don't pre-can all 9 products here).
    product: {
      teaserLabel: "Ask about this product",
      teaser: "Questions about this product? Ask me, or tell me what you're building.",
      greeting:
        "<p>You're looking at <strong>" + (PRODUCT_NAME || "this product") + "</strong>. Ask me anything about it, or tell me what you're formulating.</p>" +
        "<p>" + CONTACT_LINE + "</p>",
      chips: [
        { q: "Best applications?", llm: true },
        { q: "How is it typically used?", llm: true },
        CHIP_CERTS,
        CHIP_SPEC
      ]
    },
    match: {
      teaserLabel: "Prefer to just ask?",
      teaser: "Don't feel like the quiz? Just tell me what you're formulating.",
      greeting:
        "<p>Filling out the match quiz works — but you don't have to. Just tell me what you're making and I'll point you to the right goji format.</p>" +
        "<p>" + CONTACT_LINE + "</p><p>What are you formulating?</p>",
      chips: [
        { q: "I'm making a beverage", llm: true },
        { q: "I need whole dried berries", llm: true },
        { q: "Help me choose a format", llm: true },
        CHIP_CERTS
      ]
    },
    generic: {
      teaserLabel: "Redvia Assistant",
      teaser: "Looking for the right goji format? Ask me, or tell me what you're building.",
      greeting:
        "<p>Hi — I'm the Redvia sourcing assistant. Ask me about our goji formats and applications, or tell me what you're formulating.</p>" +
        "<p>" + CONTACT_LINE + "</p>",
      chips: [
        { q: "What products do you offer?",
          a: "<p>Nine goji formats: whole berries (Ningxia Red, Qinghai Red, Black Goji), purees (Red, Black), leaf formats (Leaf Tea, Leaf Matcha Powder), and concentrates &amp; oils (Polysaccharide Powder, Seed Oil).</p>" },
        CHIP_CERTS,
        { q: "Help me pick a format", llm: true }
      ]
    }
  };

  var FALLBACK =
    "<p>I can speak to our goji formats and their applications — but for anything spec-specific our team will follow up with accurate figures.</p>";

  // -- Icons --------------------------------------------------------
  var ICON_CHAT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-8.5 8.5 8.5 8.5 0 0 1-3.6-.8L3 21l1.9-5.4A8.5 8.5 0 1 1 21 11.5z"/></svg>';
  var ICON_SEND = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4 20-7z"/></svg>';

  // -- Build DOM ----------------------------------------------------
  var conf = CONTEXTS[CTX] || CONTEXTS.generic;
  var root = document.createElement("div");
  root.className = "rv-bot rv-bot--" + CTX;  // context modifier (e.g. rv-bot--match) for per-page styling
  root.innerHTML =
    '<button class="rv-bot__launcher" aria-label="Open Redvia assistant">' + ICON_CHAT + '<span class="rv-bot__launcher-dot"></span></button>' +
    '<div class="rv-bot__teaser" role="button" tabindex="0">' +
      '<button class="rv-bot__teaser-close" aria-label="Dismiss">&times;</button>' +
      '<span class="rv-bot__teaser-label">' + conf.teaserLabel + '</span>' + conf.teaser +
    '</div>' +
    '<div class="rv-bot__panel" role="dialog" aria-label="Redvia assistant">' +
      '<div class="rv-bot__header">' +
        '<div class="rv-bot__avatar">' + ICON_CHAT + '</div>' +
        '<div class="rv-bot__title"><h4>Redvia Assistant</h4><p><span class="rv-bot__online"></span> Sourcing help · replies in seconds</p></div>' +
        '<button class="rv-bot__close" aria-label="Close">&times;</button>' +
      '</div>' +
      '<div class="rv-bot__messages"></div>' +
      '<div class="rv-bot__chips"></div>' +
      '<div class="rv-bot__composer">' +
        '<textarea class="rv-bot__input" rows="1" placeholder="Ask about a product or your formulation..."></textarea>' +
        '<button class="rv-bot__send" aria-label="Send">' + ICON_SEND + '</button>' +
      '</div>' +
      '<div class="rv-bot__disclaimer">Preview · specs confirmed by our team.</div>' +
    '</div>';
  document.body.appendChild(root);

  var launcher = root.querySelector(".rv-bot__launcher");
  var teaser = root.querySelector(".rv-bot__teaser");
  var teaserClose = root.querySelector(".rv-bot__teaser-close");
  var closeBtn = root.querySelector(".rv-bot__close");
  var messages = root.querySelector(".rv-bot__messages");
  var chipsWrap = root.querySelector(".rv-bot__chips");
  var input = root.querySelector(".rv-bot__input");
  var sendBtn = root.querySelector(".rv-bot__send");

  var started = false;
  var HISTORY = [];

  // -- Helpers ------------------------------------------------------
  function scrollDown() { messages.scrollTop = messages.scrollHeight; }
  function addUser(text) {
    var el = document.createElement("div");
    el.className = "rv-bot__msg rv-bot__msg--user";
    el.textContent = text;
    messages.appendChild(el); scrollDown();
  }
  function addBotHTML(html, defer) {
    var el = document.createElement("div");
    el.className = "rv-bot__msg rv-bot__msg--bot";
    el.innerHTML = html + (defer ? '<span class="rv-bot__defer">' + defer + "</span>" : "");
    messages.appendChild(el); scrollDown();
  }
  function showTyping() {
    var el = document.createElement("div");
    el.className = "rv-bot__msg rv-bot__msg--bot";
    el.innerHTML = '<div class="rv-bot__typing"><span></span><span></span><span></span></div>';
    messages.appendChild(el); scrollDown();
    return el;
  }
  function offerLeadCard() {
    var card = document.createElement("div");
    card.className = "rv-bot__leadcard";
    card.innerHTML =
      "<h5>Want a brief sent to our team?</h5>" +
      "<p>I'll package what you've told me. Our team replies with specs, CoAs, and samples within 48 hours.</p>" +
      '<a class="rv-bot__leadcard-btn" href="/contact.html">Build my brief →</a>';
    messages.appendChild(card); scrollDown();
  }
  function textToHTML(t) {
    var esc = t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    return esc.split(/\n{2,}/).map(function (p) { return "<p>" + p.replace(/\n/g, "<br>") + "</p>"; }).join("");
  }

  function answerChip(chip) {
    addUser(chip.q);
    if (chip.llm) { askLLM(chip.q); return; }
    var typing = showTyping();
    setTimeout(function () {
      typing.remove();
      addBotHTML(chip.a, chip.defer);
      if (chip.offerLead) setTimeout(offerLeadCard, 350);
    }, 450);
  }

  function askLLM(text) {
    var typing = showTyping();
    HISTORY.push({ role: "user", content: text });
    fetch(BASE_API + "/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ context: CTX, productName: PRODUCT_NAME, messages: HISTORY })
    })
      .then(function (res) { return res.json(); })
      .then(function (data) {
        typing.remove();
        if (!data || data.error || !data.reply) { addBotHTML(FALLBACK); return; }
        HISTORY.push({ role: "assistant", content: data.reply });
        addBotHTML(textToHTML(data.reply));
        if (data.offerBrief) setTimeout(offerLeadCard, 350);
      })
      .catch(function () { typing.remove(); addBotHTML(FALLBACK); });
  }

  function answerCustom(text) { addUser(text); askLLM(text); }

  function renderChips() {
    chipsWrap.innerHTML = "";
    conf.chips.forEach(function (chip) {
      var b = document.createElement("button");
      b.className = "rv-bot__chip";
      b.textContent = chip.q;
      b.onclick = function () { b.remove(); answerChip(chip); };
      chipsWrap.appendChild(b);
    });
  }

  function startConversation() {
    if (started) return;
    started = true;
    var typing = showTyping();
    setTimeout(function () { typing.remove(); addBotHTML(conf.greeting); renderChips(); }, 500);
  }

  // -- Open / close -------------------------------------------------
  function openPanel() {
    root.classList.add("is-open");
    root.classList.remove("show-teaser", "has-unread");
    startConversation();
    setTimeout(function () { input.focus(); }, 350);
  }
  function closePanel() {
    root.classList.remove("is-open");
    if (STORAGE_OK) sessionStorage.setItem(SS_DISMISS, "1");  // manual close = strong "leave me alone" signal
  }

  launcher.onclick = function () { root.classList.contains("is-open") ? closePanel() : openPanel(); };
  closeBtn.onclick = closePanel;
  teaser.onclick = openPanel;
  teaser.onkeydown = function (e) { if (e.key === "Enter") openPanel(); };
  // Dismiss hides the teaser for THIS page only (not the whole session) —
  // each new page shows it fresh, so the bot keeps a light presence.
  teaserClose.onclick = function (e) { e.stopPropagation(); root.classList.remove("show-teaser"); };

  sendBtn.onclick = function () {
    var v = input.value.trim();
    if (!v) return;
    input.value = ""; input.style.height = "auto";
    answerCustom(v);
  };
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendBtn.click(); }
  });
  input.addEventListener("input", function () {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 90) + "px";
  });

  // -- Initial behavior (session-throttled) -------------------------
  var hasAutoOpened = STORAGE_OK && sessionStorage.getItem(SS_AUTO) === "1";
  var hasDismissed  = STORAGE_OK && sessionStorage.getItem(SS_DISMISS) === "1";

  if (hasDismissed) {
    // Already closed once this session: never auto-pop again, just keep the dot.
    root.classList.add("has-unread");
  } else if (FULL_OPEN && !hasAutoOpened && STORAGE_OK) {
    // First high-intent page (product/match) this session: auto-open once.
    setTimeout(function () {
      sessionStorage.setItem(SS_AUTO, "1");  // spend the session's one auto-open
      if (!root.classList.contains("is-open")) openPanel();
    }, 700);
  } else {
    // Ordinary page / already auto-opened / storage unavailable: gentle teaser.
    setTimeout(function () {
      if (!root.classList.contains("is-open")) root.classList.add("show-teaser", "has-unread");
    }, 1800);
  }
})();
