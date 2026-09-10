// Shared by the public page and the admin desk.
// The publishable key is meant to be public: row-level security in Supabase
// decides what each visitor can read or write, not this file.
window.CASEBOOK = {
  url: "https://toszglqsilzhzlsuckoa.supabase.co",
  key: "sb_publishable_0qUXMalHne2Awz4mk6eECA_THljfkci",
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (u) => (/^https:\/\//.test(u || "") ? u : "#");
const localDay = (d) => new Date(d).toLocaleDateString("en-CA");
const today = () => localDay(Date.now());

function due(iso) {
  if (!iso) return { text: "No deadline", soon: false, past: false };
  const d = new Date(iso), days = Math.ceil((d - Date.now()) / 86400000);
  const date = d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
  if (d < Date.now()) return { text: "Closed", soon: false, past: true };
  if (days <= 1) return { text: "Closes today", soon: true, past: false, date };
  return { text: days <= 3 ? `${days} days left` : `Due ${date}`, soon: days <= 3, past: false, date };
}

function listingRow(d, updated) {
  const t = due(d.deadline);
  return `<li class="row">
    <div class="row-top"><div>${updated ? `<div class="updated">Updated</div>` : ""}<h3 class="title">${esc(d.title)}</h3><div class="org">${esc(d.org)}</div></div>
      <span class="due ${t.soon ? "soon" : ""}">${esc(t.text)}</span></div>
    <div class="facts"><span class="money">${esc(d.prizes)}</span><span>${esc(d.fee)}</span><span>${esc(d.eligibility)}</span></div>
    <a class="link" href="${esc(safeUrl(d.url))}" target="_blank" rel="noopener">Register on Unstop →</a>
  </li>`;
}

let toastTimer;
function toast(msg, undo) {
  const el = $("toast");
  el.innerHTML = `<span>${esc(msg)}</span>${undo ? `<button id="undo">Undo</button>` : ""}`;
  el.hidden = false;
  if (undo) $("undo").onclick = () => { el.hidden = true; undo(); };
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 5000);
}
