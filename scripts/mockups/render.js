// SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
// Copyright 2026 ScalarOps Technologies LLC
//
// Render a design-canvas mockup (a .dc.html artboard) to one self-contained static HTML page.
// It runs the artboard's renderVals() with the given props, expands <sc-for> and <sc-if>, fills
// {{holes}}, drops event handlers, and leaves out web-font links so the page works offline.
//
//   node scripts/mockups/render.js <in.dc.html> <out.html> ['{"theme":"dark","tab":"agents"}'] ['{"Main.dc.html":"overview.html"}']
//
// The third argument is the props (the artboard's data-props tweaks); the fourth maps artboard
// links to output file names. See docs/tooling/mockup-standard.md.
'use strict';
const fs = require('fs');

const [, , inPath, outPath, propsJson, hrefsJson] = process.argv;
if (!inPath || !outPath) {
  process.stderr.write('usage: render.js <in.dc.html> <out.html> [props-json] [href-map-json]\n');
  process.exit(2);
}
const src = fs.readFileSync(inPath, 'utf8');
const props = JSON.parse(propsJson || '{}');
const hrefs = JSON.parse(hrefsJson || '{}');

const script = src.match(/<script type="text\/x-dc"[^>]*>([\s\S]*?)<\/script>/);
if (!script) throw new Error(`${inPath}: no <script type="text/x-dc"> block`);
class DCLogic {
  constructor(p) { this.props = p || {}; this.state = {}; }
  setState() {}
  forceUpdate() {}
}
// Auto theme resolves to dark when rendered here: there is no system preference to read.
const fakeWindow = { matchMedia: () => ({ matches: true }) };
const Component = new Function('DCLogic', 'window', `${script[1]}\nreturn Component;`)(DCLogic, fakeWindow);
const inst = new Component(props);
inst.props = props;
const vals = inst.renderVals();

const title = (src.match(/<title>([\s\S]*?)<\/title>/) || [, 'Mockup'])[1];
const xdc = src.match(/<x-dc>([\s\S]*?)<\/x-dc>/);
if (!xdc) throw new Error(`${inPath}: no <x-dc> block`);
const helmet = (xdc[1].match(/<helmet>([\s\S]*?)<\/helmet>/) || [, ''])[1]
  .replace(/<link[^>]*fonts\.(googleapis|gstatic)[^>]*>\s*/g, '');
let body = xdc[1].replace(/<helmet>[\s\S]*?<\/helmet>/, '');

const lookup = (scope, path) => path.trim().split('.').reduce((o, k) => (o == null ? undefined : o[k]), scope);
const holes = (s, scope) => s.replace(/\{\{\s*([\w.$]+)\s*\}\}/g, (_, p) => {
  const v = lookup(scope, p);
  return v == null || typeof v === 'function' ? '' : String(v);
});

function block(s, start, tag) {
  const tagEnd = s.indexOf('>', start);
  const open = new RegExp(`<${tag}\\b`);
  const close = `</${tag}>`;
  let depth = 1;
  let i = tagEnd + 1;
  while (depth) {
    const rest = s.slice(i);
    const o = rest.search(open);
    const c = rest.indexOf(close);
    if (c < 0) throw new Error(`unclosed <${tag}>`);
    if (o >= 0 && o < c) { depth++; i += o + tag.length + 1; } else { depth--; i += c + close.length; }
  }
  return { tag: s.slice(start, tagEnd + 1), inner: s.slice(tagEnd + 1, i - close.length), end: i };
}

function expand(s, scope) {
  let out = '';
  for (;;) {
    const m = s.match(/<sc-(for|if)\b/);
    if (!m) return out + holes(s, scope);
    out += holes(s.slice(0, m.index), scope);
    const b = block(s, m.index, `sc-${m[1]}`);
    if (m[1] === 'for') {
      const list = lookup(scope, b.tag.match(/list="\{\{\s*([\w.$]+)\s*\}\}"/)[1]) || [];
      const as = b.tag.match(/as="(\w+)"/)[1];
      list.forEach((item, idx) => { out += expand(b.inner, { ...scope, [as]: item, $index: idx }); });
    } else if (lookup(scope, b.tag.match(/value="\{\{\s*([\w.$]+)\s*\}\}"/)[1])) {
      out += expand(b.inner, scope);
    }
    s = s.slice(b.end);
  }
}

body = expand(body, vals).replace(/\s+on[A-Z]\w*="[^"]*"/g, '');
for (const [from, to] of Object.entries(hrefs)) body = body.split(`href="${from}"`).join(`href="${to}"`);
fs.writeFileSync(outPath, `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${title}</title>
${helmet.trim()}
</head>
<body>
${body.trim()}
</body>
</html>
`);
