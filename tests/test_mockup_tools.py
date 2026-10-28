# SPDX-License-Identifier: LicenseRef-PolyForm-Shield-1.0.0
# Copyright 2026 ScalarOps Technologies LLC
"""The mockup tooling (issue #235): render.js expands a design-canvas artboard, check.py reads its variants."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "scripts" / "mockups" / "render.js"

FIXTURE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Fixture</title><script src="./support.js"></script></head>
<body>
<x-dc>
<helmet><link href="https://fonts.googleapis.com/css2?family=X" rel="stylesheet"><style>body{margin:0}</style></helmet>
<div style="background: {{c.bg}}">
  <sc-if value="{{is.list}}" hint-placeholder-val="{{true}}">
    <ul><sc-for list="{{rows}}" as="r" hint-placeholder-count="2"><li onClick="{{r.pick}}">{{r.name}}:<sc-for list="{{r.tags}}" as="t"><b>{{t}}</b></sc-for></li></sc-for></ul>
  </sc-if>
  <sc-if value="{{is.empty}}" hint-placeholder-val="{{false}}"><p>Nothing here</p></sc-if>
  <a href="Other.dc.html">other</a>
  <sc-for list="{{opts}}" as="o"><input type="checkbox" checked="{{o.on}}" name="{{o.name}}"></sc-for>
</div>
</x-dc>
<script type="text/x-dc" data-dc-script data-props='{"theme":{"editor":"enum","options":["dark","light"],"default":"dark"},"tab":{"editor":"enum","options":["list","empty"],"default":"list"},"$preview":{"width":400,"height":300}}'>
class Component extends DCLogic {
  renderVals() {
    const dark = (this.props.theme || 'dark') === 'dark';
    const tab = this.props.tab || 'list';
    return {
      c: { bg: dark ? '#0F1216' : '#FFFFFF' },
      is: { list: tab === 'list', empty: tab === 'empty' },
      rows: [{ name: 'a', tags: ['x', 'y'], pick: () => 1 }, { name: 'b', tags: [] }],
      opts: [{ name: 'on', on: true }, { name: 'off', on: null }, { name: 'no', on: false }]
    };
  }
}
</script>
</body></html>
"""


def load_check():
    spec = importlib.util.spec_from_file_location("mockup_check", ROOT / "scripts" / "mockups" / "check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CheckReadsTheVariants(unittest.TestCase):
    def test_the_enum_tweaks_are_the_variants(self):
        self.assertEqual(load_check().enum_props(FIXTURE), {"theme": ["dark", "light"], "tab": ["list", "empty"]})

    def test_no_data_props_means_one_page(self):
        self.assertEqual(load_check().enum_props("<html></html>"), {})


@unittest.skipUnless(shutil.which("node"), "Node isn't installed")
class RenderExpandsTheArtboard(unittest.TestCase):
    def render(self, props: dict, hrefs: dict | None = None) -> str:
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d, "Main.dc.html"), Path(d, "out.html")
            src.write_text(FIXTURE)
            subprocess.run(["node", str(RENDER), str(src), str(out), json.dumps(props), json.dumps(hrefs or {})],
                           check=True)
            return out.read_text()

    def test_loops_conditions_and_holes(self):
        html = self.render({"theme": "light", "tab": "list"})
        self.assertIn("background: #FFFFFF", html)
        self.assertIn("<li>a:<b>x</b><b>y</b></li>", html)
        self.assertIn("<li>b:</li>", html)
        self.assertNotIn("Nothing here", html)
        for leftover in ("{{", "<sc-", "onClick", "undefined", "fonts.googleapis"):
            self.assertNotIn(leftover, html)

    def test_a_boolean_attribute_is_written_only_when_its_value_is_truthy(self):
        # checked="" would still check the box: HTML reads a boolean attribute's presence, not its value.
        html = self.render({"theme": "light", "tab": "list"})
        self.assertIn('<input type="checkbox" checked name="on">', html)
        self.assertIn('<input type="checkbox" name="off">', html)
        self.assertIn('<input type="checkbox" name="no">', html)

    def test_props_pick_the_branch_and_links_are_mapped(self):
        html = self.render({"theme": "dark", "tab": "empty"}, {"Other.dc.html": "other.html"})
        self.assertIn("background: #0F1216", html)
        self.assertIn("Nothing here", html)
        self.assertNotIn("<ul>", html)
        self.assertIn('href="other.html"', html)


if __name__ == "__main__":
    unittest.main()
