// Small, safe markdown renderer: everything is HTML-escaped first, then a known subset is formatted.
// Source citations like [D3 p.12] or [D3, D7 p.2] become clickable links (class "doc", data-id, data-page).
(function () {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const CITE = /\[(\s*D\d+[^\]\n]{0,80})\]/g;
  const REF = /\bD(\d+)(?:\s*,?\s*(?:p\.?|pp\.?|page)\s*(\d+)(?:\s*[-–]\s*\d+)?)?/gi;
  const docLink = (id, page) =>
    `<a class="doc" data-id="${id}"${page ? ` data-page="${page}"` : ""} title="Open D${id}${page ? `, page ${page}` : ""}">D${id}${page ? ` p.${page}` : ""}</a>`;

  function inline(text) {
    const slots = [];
    const hold = (html) => `\u0000${slots.push(html) - 1}\u0000`;
    let s = esc(text);
    s = s.replace(/`([^`]+)`/g, (_, c) => hold(`<code>${c}</code>`));
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, t, u) => hold(`<a href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`));
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
      (_, pre, u) => pre + hold(`<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`));
    s = s.replace(CITE, (_, inner) =>
      hold(`<span class="cite">${inner.replace(REF, (__, id, page) => docLink(id, page))}</span>`));
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_]+)__/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => slots[+i]);
  }

  const splitRow = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

  function render(src) {
    const lines = src.replace(/\r/g, "").split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      let m;
      if ((m = line.match(/^\s*```(\w*)/))) {                       // fenced code
        const buf = []; i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
        i++;
        out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`);
      } else if ((m = line.match(/^(#{1,6})\s+(.*)/))) {             // heading
        const n = Math.min(m[1].length + 1, 4);
        out.push(`<h${n}>${inline(m[2])}</h${n}>`); i++;
      } else if (/^\s*([-*_])\s*\1\s*\1[\s\1]*$/.test(line)) {       // hr
        out.push("<hr>"); i++;
      } else if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        const head = splitRow(line); i += 2;                          // table
        const rows = [];
        // trailing "|" optional so a row still being streamed renders as a row, not a paragraph
        while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(splitRow(lines[i++]));
        out.push(`<div class="table-wrap"><table><thead><tr>${head.map((h) => `<th>${inline(h)}</th>`).join("")}</tr></thead><tbody>` +
          rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("") + "</tbody></table></div>");
      } else if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {              // lists (nesting by indent)
        const stack = [];
        const ITEM = /^\s*([-*+]|\d+[.)])\s+/;
        while (i < lines.length && (ITEM.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && stack.length) ||
               (!lines[i].trim() && stack.length && i + 1 < lines.length && (ITEM.test(lines[i + 1]) || /^\s{2,}\S/.test(lines[i + 1]))))) {
          const l = lines[i];
          if (!l.trim()) { i++; continue; }                           // blank line inside a loose list
          const lm = l.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)/);
          if (!lm) { out.push(" " + inline(l.trim())); i++; continue; }  // continuation line
          const indent = lm[1].length, tag = /\d/.test(lm[2]) ? "ol" : "ul";
          while (stack.length && indent < stack[stack.length - 1].indent) out.push(`</li></${stack.pop().tag}>`);
          const top = stack[stack.length - 1];
          if (!top || indent > top.indent) {
            const start = tag === "ol" && parseInt(lm[2], 10) !== 1 ? ` start="${parseInt(lm[2], 10)}"` : "";
            out.push(`<${tag}${start}><li>`); stack.push({ indent, tag });
          }
          else out.push("</li><li>");
          out.push(inline(lm[3])); i++;
        }
        while (stack.length) out.push(`</li></${stack.pop().tag}>`);
      } else if (/^\s*>/.test(line)) {                               // blockquote
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
        out.push(`<blockquote>${render(buf.join("\n"))}</blockquote>`);
      } else if (!line.trim()) {
        i++;
      } else {                                                        // paragraph
        const buf = [];
        while (i < lines.length && lines[i].trim() && !/^(\s*```|#{1,6}\s|\s*([-*+]|\d+[.)])\s+|\s*>|\s*\|.*\|\s*$)/.test(lines[i]))
          buf.push(lines[i++]);
        if (!buf.length) buf.push(lines[i++]);
        out.push(`<p>${buf.map(inline).join("<br>")}</p>`);
      }
    }
    return out.join("");
  }

  window.renderMarkdown = render;
  window.docLink = docLink;
  window.escapeHtml = esc;
})();
