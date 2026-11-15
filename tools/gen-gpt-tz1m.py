#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate GPT-T from the ORIGINAL GPT of board #6: 'tz' grows to 1 MiB and swallows 'tzbak' (entry removed).
Every other entry is byte-identical and keeps its LBAs. Reuses scripts/gptool.py (build/load_gpt/CRCs).
usage: python3 -I tools/gen-gpt-tz1m.py --primary P --emmc N --outdir D     (never touches a device)"""
import argparse, importlib.util, os, struct, sys
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location('gptool', os.path.join(root, 'scripts', 'gptool.py'))
G = importlib.util.module_from_spec(spec); spec.loader.exec_module(G)
ap = argparse.ArgumentParser(); ap.add_argument('--primary', required=True); ap.add_argument('--emmc', type=int, required=True)
ap.add_argument('--outdir', required=True); a = ap.parse_args()
tpl = G.load_gpt(open(a.primary, 'rb').read(), 'primary', a.emmc); G.validate_template(tpl, a.emmc)
ents = G.used(tpl); by = {e['name']: e for e in ents}
tz, tzb = by['tz'], by['tzbak']
if tzb['start'] != tz['end'] + 1 or (tz['end'] - tz['start'] + 1) != 1024 or (tzb['end'] - tzb['start'] + 1) != 1024:
    raise SystemExit('unexpected tz/tzbak geometry')
out = []
for e in ents:
    if e['name'] == 'tzbak': continue
    raw = e['raw']
    if e['name'] == 'tz': raw = raw[:40] + struct.pack('<Q', tzb['end']) + raw[48:]
    out.append(raw)
prim, sec = G.build(tpl, out, a.emmc)
os.makedirs(a.outdir, exist_ok=True)
open(os.path.join(a.outdir, 'gpt-T-primary.bin'), 'wb').write(prim)
open(os.path.join(a.outdir, 'gpt-T-secondary.bin'), 'wb').write(sec)
print(G.fmt_table(G.load_gpt(prim, 'primary', a.emmc)))
