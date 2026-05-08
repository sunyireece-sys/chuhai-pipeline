async function translateEmail(btn) {
  const form = btn.closest(".send-form");
  if (!form) return;

  const statusEl = form.querySelector(".translate-status");
  const langEl = form.querySelector(".lang-select");
  const lang = langEl ? langEl.value : "";
  if (!lang) {
    if (statusEl) statusEl.textContent = "请选择语言";
    return;
  }

  const subjectEl = form.querySelector('input[name="subject"]');
  const bodyEl = form.querySelector('textarea[name="body"]');
  const followupEl = form.querySelector('textarea[name="follow_up"]');
  if (statusEl) statusEl.textContent = "翻译中...";
  btn.disabled = true;

  async function doTranslate(text) {
    if (!text) return text;
    const fd = new FormData();
    fd.append("text", text);
    fd.append("target_lang", lang);
    const response = await fetch("/translate", { method: "POST", body: fd });
    if (!response.ok) throw new Error(await response.text());
    const payload = await response.json();
    return payload.translated || "";
  }

  try {
    const [subject, body, followup] = await Promise.all([
      doTranslate(subjectEl ? subjectEl.value : ""),
      doTranslate(bodyEl ? bodyEl.value : ""),
      doTranslate(followupEl ? followupEl.value : ""),
    ]);
    if (subjectEl) subjectEl.value = subject;
    if (bodyEl) bodyEl.value = body;
    if (followupEl) followupEl.value = followup;
    if (statusEl) {
      statusEl.textContent = "已翻译为" + langEl.options[langEl.selectedIndex].text;
    }
  } catch (err) {
    if (statusEl) statusEl.textContent = "翻译失败，请重试";
  } finally {
    btn.disabled = false;
  }
}
