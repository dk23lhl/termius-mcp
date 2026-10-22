"""Bridge installation into the renderer, run under Node with a fake window: a second
install (another MCP process, or another adapter version) must never replace the bridge
object whose closures hold in-flight captures and upload records."""
import json
import shutil
import subprocess

import pytest

import server

NODE = shutil.which("node")

RUNNER = r"""
const vm = require('vm');
const scripts = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const window = {__kc: 'upstream'};
const ctx = vm.createContext({window, document: {getElementById: () => null}, Node: class {}, console});
const out = [];
for (const s of scripts) {
  try { vm.runInContext(s, ctx); out.push({ok: true}); }
  catch (e) { out.push({ok: false, error: String(e.message)}); }
  const all = window.__kcBridges || {};
  out[out.length - 1].ids = Object.keys(all);
  out[out.length - 1].kc = window.__kc;
  out[out.length - 1].full = Object.fromEntries(Object.entries(all).map(([k, v]) =>
    [k, typeof v.startCapture === 'function' && typeof v.startUpload === 'function']));
  if (!window.first && all.A) window.first = all.A;
  out[out.length - 1].sameA = !!all.A && all.A === window.first;
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node is required to evaluate the bridge")
def test_install_is_idempotent_and_versions_do_not_replace_each_other():
    scripts = [server._install_script("A", server.BRIDGE),
               server._install_script("A", server.BRIDGE),       # another process, same version
               server._install_script("B", server.BRIDGE),       # another version
               server._install_script("C", "throw new Error('boom')")]
    res = subprocess.run([NODE, "-e", RUNNER], input=json.dumps(scripts),
                         capture_output=True, text=True, timeout=30, check=True)
    first, again, other, broken = json.loads(res.stdout)
    assert first["ok"] and first["ids"] == ["A"] and first["full"] == {"A": True}
    assert again["ok"] and again["ids"] == ["A"] and again["sameA"]
    assert other["ok"] and other["ids"] == ["A", "B"] and other["sameA"]
    assert not broken["ok"] and "boom" in broken["error"] and broken["ids"] == ["A", "B"]
    # The upstream adapter's global bridge is left alone throughout.
    assert {r["kc"] for r in (first, again, other, broken)} == {"upstream"}


def test_calls_address_this_versions_bridge():
    assert server.KC == f'window.__kcBridges["{server.BRIDGE_ID}"]'
    assert server.INSTALL == server._install_script(server.BRIDGE_ID, server.BRIDGE)
