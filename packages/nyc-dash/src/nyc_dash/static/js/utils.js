/*
 * Shared, dependency-free utilities. Every other script on this page loads after this
 * one and may use anything here; this file must never depend on anything else.
 */

function el(id) {
  return document.getElementById(id);
}

function hhmmss(iso) {
  if (!iso) return "unknown time";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString();
}

const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

// Upstream text (restaurant names, 311 descriptors, camera names, GTFS stop names) is
// never trusted as markup: every detail panel and tooltip runs interpolated values
// through this before landing in innerHTML.
function escapeHtml(value) {
  if (value == null) return "";
  return String(value).replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch]);
}

function banner(message) {
  const node = el("banner");
  node.textContent = message;
  node.hidden = false;
}

function setConnection(mode, text) {
  const node = el("connection-badge");
  node.dataset.mode = mode;
  node.textContent = text;
}
