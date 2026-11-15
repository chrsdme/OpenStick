#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""GPT-B generator and checker for MSM8916 sticks (standard library only, never touches a device).

Run as:  python3 -I scripts/gptool.py <command> ...   (called by gen-gpt.sh / tests / tools)

The generator is TEMPLATE BASED: the entries that are kept are copied byte-identical (128 B each) from a
local GPT dump of the same board (primary, 34 sectors). Only the Android-only partitions are replaced:
  dropped : boot (old, 16 MiB), system, cache, recovery, userdata
  added   : boot (new, at the old 'system' start), rootfs (at the old 'cache' start, to the last usable LBA)
The table stays dense (no holes) and keeps the template header fields (disk GUID, entry count 28, entry
size 128, first/last usable). Unique GUIDs of the new entries come from the caller (BOOT/ROOT_PARTUUID).
Output never prints unique GUIDs, only names and LBAs.
"""
import argparse
import os
import struct
import sys
import uuid
import zlib

SS = 512
HDR = '<8sIIIIQQQQ16sQIII'          # 92 bytes
HDR_SZ = struct.calcsize(HDR)
ESZ = 128
SIG = b'EFI PART'

DROP = ('boot', 'system', 'cache', 'recovery', 'userdata')
CALIBRATION = ('modemst1', 'modemst2', 'fsc', 'ssd', 'DDR', 'fsg', 'sec', 'persist', 'splash')
BOOT_CHAIN = ('sbl1', 'sbl1bak', 'rpm', 'rpmbak', 'tz', 'tzbak', 'pad')
OTHER_KEPT = ('modem', 'aboot', 'abootbak', 'hyp', 'hypbak', 'misc')
BOOT_TYPE = '20117F86-E985-4357-B9EE-374BC1D8487D'      # same type GUID as the stock 'boot'
ROOT_TYPE = '1B81E7E6-F50D-419B-A739-2AEEF8DA3335'      # Linux root (arm64)
BOOT_SECTORS_DEFAULT = 131072                           # 64 MiB
ALIGN = 8                                               # sectors = 4096 B


class GptError(Exception):
    pass


def u16name(raw):
    return raw[56:128].decode('utf-16le').rstrip('\0')


def mk_entry(type_guid, uniq_guid, start, end, attrs, name):
    nm = name.encode('utf-16le')
    if len(nm) > 72:
        raise GptError('name too long: ' + name)
    return (uuid.UUID(type_guid).bytes_le + uuid.UUID(uniq_guid).bytes_le +
            struct.pack('<QQQ', start, end, attrs) + nm.ljust(72, b'\0'))


def ent_fields(raw):
    t, u, a, b, at = struct.unpack('<16s16sQQQ', raw[:56])
    return {'name': u16name(raw), 'type': t, 'uniq': u, 'start': a, 'end': b, 'attrs': at, 'raw': raw}


def parse_hdr(sector):
    f = struct.unpack(HDR, sector[:HDR_SZ])
    keys = ('sig', 'rev', 'hsz', 'crc', 'res', 'my', 'alt', 'first', 'last', 'guid', 'elba', 'n', 'esz', 'ecrc')
    return dict(zip(keys, f))


def hdr_crc_ok(sector):
    h = parse_hdr(sector)
    if h['hsz'] != HDR_SZ:
        return False
    b = bytearray(sector[:h['hsz']])
    b[16:20] = b'\0\0\0\0'
    return zlib.crc32(bytes(b)) & 0xffffffff == h['crc']


def make_hdr(base_sector, my, alt, elba, ecrc):
    h = parse_hdr(base_sector)
    raw = bytearray(struct.pack(HDR, h['sig'], h['rev'], h['hsz'], 0, h['res'], my, alt, h['first'], h['last'],
                                h['guid'], elba, h['n'], h['esz'], ecrc))
    crc = zlib.crc32(bytes(raw)) & 0xffffffff
    raw[16:20] = struct.pack('<I', crc)
    return bytes(raw) + base_sector[HDR_SZ:SS]


def load_gpt(buf, kind, emmc):
    """Parse a primary (34 sectors) or secondary (33 sectors) dump. Returns dict, raises GptError."""
    if len(buf) % SS or len(buf) < 3 * SS:
        raise GptError('%s: bad size %d' % (kind, len(buf)))
    reg = len(buf) // SS - 2 if kind == 'primary' else len(buf) // SS - 1
    if kind == 'primary':
        hsec, region = buf[SS:2 * SS], buf[2 * SS:]
    else:
        region, hsec = buf[:reg * SS], buf[reg * SS:]
    h = parse_hdr(hsec)
    if h['sig'] != SIG:
        raise GptError('%s: bad signature' % kind)
    if h['esz'] != ESZ or h['n'] * h['esz'] > len(region):
        raise GptError('%s: unsupported entry size/count (%d x %d)' % (kind, h['n'], h['esz']))
    tab = region[:h['n'] * h['esz']]
    ents = []
    for i in range(h['n']):
        raw = tab[i * ESZ:(i + 1) * ESZ]
        ents.append(None if raw == b'\0' * ESZ else ent_fields(raw))
    return {'kind': kind, 'hsec': hsec, 'hdr': h, 'region': region, 'tab': tab, 'entries': ents,
            'mbr': buf[:SS] if kind == 'primary' else None, 'reg_sectors': reg}


def used(g):
    return [e for e in g['entries'] if e]


def dense(g):
    seen_empty = False
    for e in g['entries']:
        if e is None:
            seen_empty = True
        elif seen_empty:
            return False
    return True


def validate_template(g, emmc):
    h = g['hdr']
    if g['mbr'][510:512] != b'\x55\xaa' or g['mbr'][450] != 0xee:
        raise GptError('template: sector 0 is not a protective MBR')
    if not hdr_crc_ok(g['hsec']):
        raise GptError('template: header CRC invalid')
    if zlib.crc32(g['tab']) & 0xffffffff != h['ecrc']:
        raise GptError('template: entries CRC invalid')
    if (h['my'], h['elba']) != (1, 2):
        raise GptError('template: not a primary header (my=%d entries=%d)' % (h['my'], h['elba']))
    if h['alt'] != emmc - 1 or h['last'] != emmc - 1 - g['reg_sectors'] - 1 or h['first'] != 2 + g['reg_sectors']:
        raise GptError('template does not match EMMC_SECTORS=%d (backup LBA %d, last usable %d)' %
                       (emmc, h['alt'], h['last']))
    if not dense(g):
        raise GptError('template: entry table has holes')
    if any(g['region'][h['n'] * ESZ:]):
        raise GptError('template: non-zero bytes after the entry array')


def build(tpl, entries_raw, emmc):
    """entries_raw: list of 128 B blobs (dense). Returns (primary_bytes, secondary_bytes)."""
    h = tpl['hdr']
    if len(entries_raw) > h['n']:
        raise GptError('too many entries (%d > %d)' % (len(entries_raw), h['n']))
    tab = b''.join(entries_raw).ljust(h['n'] * ESZ, b'\0')
    ecrc = zlib.crc32(tab) & 0xffffffff
    region = tab.ljust(tpl['reg_sectors'] * SS, b'\0')
    prim = tpl['mbr'] + make_hdr(tpl['hsec'], 1, emmc - 1, 2, ecrc) + region
    sec_elba = emmc - 1 - tpl['reg_sectors']
    sec = region + make_hdr(tpl['hsec'], emmc - 1, 1, sec_elba, ecrc)
    return prim, sec


def plan_gptb(tpl, emmc, boot_uuid, root_uuid, boot_sectors):
    ents = used(tpl)
    byname = {e['name']: e for e in ents}
    for n in DROP + ('persist',):
        if n not in byname:
            raise GptError("template has no partition '%s'" % n)
    sysd, cache, persist = byname['system'], byname['cache'], byname['persist']
    boot_start = sysd['start']
    boot_end = boot_start + boot_sectors - 1
    if boot_end > sysd['end']:
        raise GptError('new boot (%d..%d) does not fit in the old system range' % (boot_start, boot_end))
    root_start = cache['start']
    if root_start != persist['end'] + 1:
        raise GptError('unexpected layout: cache does not follow persist')
    last = tpl['hdr']['last']
    root_sectors = (last - root_start + 1) // ALIGN * ALIGN
    root_end = root_start + root_sectors - 1
    if boot_start % ALIGN or root_start % ALIGN:
        raise GptError('start LBA not 4 KiB aligned')
    out, new_boot, new_root = [], None, None
    new_boot = mk_entry(BOOT_TYPE, boot_uuid, boot_start, boot_end, 0, 'boot')
    new_root = mk_entry(ROOT_TYPE, root_uuid, root_start, root_end, 0, 'rootfs')
    for e in ents:
        if e['name'] == 'boot':
            out.append(new_boot)
        elif e['name'] in DROP:
            continue
        else:
            out.append(e['raw'])
    out.append(new_root)
    return out


def fmt_table(g):
    rows = ['%-3s %-10s %9s %9s %9s' % ('#', 'name', 'start', 'end', 'sectors')]
    for i, e in enumerate(g['entries']):
        if e:
            rows.append('%-3d %-10s %9d %9d %9d' % (i + 1, e['name'], e['start'], e['end'], e['end'] - e['start'] + 1))
    return '\n'.join(rows)


def sparse_raw_size(path):
    with open(path, 'rb') as f:
        h = f.read(28)
    if len(h) == 28 and struct.unpack('<I', h[:4])[0] == 0xED26FF3A:
        blk, total = struct.unpack('<II', h[12:20])
        return blk * total
    return os.path.getsize(path)


# ------------------------------------------------------------------ checks

class Ctx:
    def __init__(self, prim, sec, tpl, emmc, boot_img, root_img, boot_uuid, root_uuid, expect):
        self.prim, self.sec, self.tpl, self.emmc = prim, sec, tpl, emmc
        self.boot_img, self.root_img = boot_img, root_img      # raw sizes in bytes (or None)
        self.boot_uuid, self.root_uuid, self.expect = boot_uuid, root_uuid, expect


def run_checks(c):
    R = []

    def add(cid, ok, text):
        R.append((cid, 'OK' if ok else 'FAIL', text))

    def skip(cid, text):
        R.append((cid, 'SKIP', text))

    try:
        p = load_gpt(c.prim, 'primary', c.emmc)
        s = load_gpt(c.sec, 'secondary', c.emmc)
    except GptError as ex:
        add('parse', False, str(ex))
        return R
    ph, sh = p['hdr'], s['hdr']
    add('size', len(c.prim) == 34 * SS and len(c.sec) == 33 * SS,
        'primary %d B (34 sectors), secondary %d B (33 sectors)' % (len(c.prim), len(c.sec)))
    add('crc-primary', hdr_crc_ok(p['hsec']) and zlib.crc32(p['tab']) & 0xffffffff == ph['ecrc'],
        'primary header CRC and entries CRC valid')
    add('crc-secondary', hdr_crc_ok(s['hsec']) and zlib.crc32(s['tab']) & 0xffffffff == sh['ecrc'],
        'secondary header CRC and entries CRC valid')
    add('header-fields',
        ph['my'] == 1 and ph['alt'] == c.emmc - 1 and ph['elba'] == 2 and ph['first'] == 34 and
        ph['last'] == c.emmc - 34 and sh['my'] == c.emmc - 1 and sh['alt'] == 1 and sh['elba'] == c.emmc - 33,
        'header LBAs consistent with EMMC_SECTORS=%d (backup %d, entries %d, usable 34..%d)' %
        (c.emmc, c.emmc - 1, c.emmc - 33, c.emmc - 34))
    same = (p['tab'] == s['tab'] and ph['ecrc'] == sh['ecrc'] and ph['guid'] == sh['guid'] and
            (ph['first'], ph['last'], ph['n'], ph['esz']) == (sh['first'], sh['last'], sh['n'], sh['esz']))
    add('secondary-matches', same, 'secondary entries, disk GUID, usable range and counts equal the primary')
    mbr = c.prim[:SS]
    add('mbr', mbr[510:512] == b'\x55\xaa' and mbr[450] == 0xee and
        struct.unpack('<I', mbr[454:458])[0] == 1, 'protective MBR (type 0xEE, start LBA 1, 0x55AA)')

    ents = used(p)
    spans = sorted((e['start'], e['end'], e['name']) for e in ents)
    bad = [n for (a, b, n) in spans if a < ph['first'] or b > ph['last'] or b < a or b >= c.emmc]
    add('bounds', not bad, 'every partition within %d..%d (disk %d sectors)%s' %
        (ph['first'], ph['last'], c.emmc, ' BAD: ' + ','.join(bad) if bad else ''))
    ov = [(spans[i][2], spans[i + 1][2]) for i in range(len(spans) - 1) if spans[i][1] >= spans[i + 1][0]]
    add('no-overlap', not ov, 'no partition overlaps another%s' % (' BAD: %s' % ov if ov else ''))
    names = [e['name'] for e in ents]
    add('unique-names', len(set(names)) == len(names), 'partition names unique')

    byname = {e['name']: e for e in ents}
    nb = [byname.get('boot'), byname.get('rootfs')]
    al = all(e and e['start'] % ALIGN == 0 and (e['end'] - e['start'] + 1) % ALIGN == 0 for e in nb)
    add('alignment', al, 'new boot and rootfs start and size are multiples of 8 sectors (4096 B)')

    for nm, img, label in (('boot', c.boot_img, 'boot'), ('rootfs', c.root_img, 'rootfs')):
        e = byname.get(nm)
        if img is None:
            skip('image-' + nm, '%s image missing, not checked' % label)
        else:
            psz = (e['end'] - e['start'] + 1) * SS if e else -1
            add('image-' + nm, e is not None and img <= psz and img % 4096 == 0,
                '%s raw image %d B <= partition %d B and multiple of 4096' % (label, img, psz))

    if c.boot_uuid and c.root_uuid and nb[0] and nb[1]:
        ok = (str(uuid.UUID(bytes_le=nb[0]['uniq'])).lower() == c.boot_uuid.lower() and
              str(uuid.UUID(bytes_le=nb[1]['uniq'])).lower() == c.root_uuid.lower())
        add('partuuid', ok, 'GPT unique GUIDs of boot/rootfs equal BOOT_PARTUUID/ROOT_PARTUUID (used by extlinux, fstab)')
    else:
        skip('partuuid', 'BOOT_PARTUUID/ROOT_PARTUUID not given')

    if c.expect:
        ok = True
        for nm, (st, ln) in c.expect.items():
            e = byname.get(nm)
            ok = ok and e is not None and e['start'] == st and e['end'] - e['start'] + 1 == ln
        add('expected-gptb', ok, 'GPT-B numbers from FLASH-PLAN: ' +
            ', '.join('%s %d/%d' % (k, v[0], v[1]) for k, v in c.expect.items()))

    if c.tpl is None:
        for cid in ('preserved-identical', 'only-gptb-changes', 'calibration-unchanged', 'boot-chain-unchanged'):
            skip(cid, 'no GPT template (BOARD_GPT_TEMPLATE), not checked')
        return R
    t = c.tpl
    tb = {e['name']: e for e in used(t)}
    kept = [e for e in used(t) if e['name'] not in DROP]
    miss = [e['name'] for e in kept if e['raw'] not in [x['raw'] for x in ents]]
    add('preserved-identical', not miss,
        '%d kept template entries are byte-identical in the new table%s' % (len(kept), ' BAD: ' + ','.join(miss) if miss else ''))
    tr = set(e['raw'] for e in used(t))
    extra = sorted(e['name'] for e in ents if e['raw'] not in tr)
    add('only-gptb-changes', extra == sorted(['boot', 'rootfs']),
        'entries not in the template are exactly: boot, rootfs (found: %s)' % ','.join(extra))
    for cid, group, label in (('calibration-unchanged', CALIBRATION, 'calibration'), ('boot-chain-unchanged', BOOT_CHAIN, 'boot chain')):
        bad = [n for n in group if n not in tb or n not in byname or byname[n]['raw'] != tb[n]['raw']]
        add(cid, not bad, '%s partitions unchanged (%s)%s' % (label, ' '.join(group), ' BAD: ' + ','.join(bad) if bad else ''))
    return R


def print_results(R):
    fail = 0
    for cid, st, text in R:
        print('%-4s %s' % (st, text))
        fail += st == 'FAIL'
    return fail


# ------------------------------------------------------------------ commands

def parse_expect(s):
    out = {}
    for part in filter(None, (s or '').split(',')):
        nm, rest = part.split('=')
        a, b = rest.split(':')
        out[nm] = (int(a), int(b))
    return out


def read(path):
    with open(path, 'rb') as f:
        return f.read()


def cmd_generate(a):
    tpl = load_gpt(read(a.template), 'primary', a.emmc)
    validate_template(tpl, a.emmc)
    ents = plan_gptb(tpl, a.emmc, a.boot_uuid, a.root_uuid, a.boot_sectors)
    prim, sec = build(tpl, ents, a.emmc)
    os.makedirs(a.outdir, exist_ok=True)
    for name, data in (('gpt-primary.bin', prim), ('gpt-secondary.bin', sec), ('gpt_both0.bin', prim + sec)):
        with open(os.path.join(a.outdir, name), 'wb') as f:
            f.write(data)
    g = load_gpt(prim, 'primary', a.emmc)
    b = [e for e in used(g) if e['name'] == 'boot'][0]
    r = [e for e in used(g) if e['name'] == 'rootfs'][0]
    with open(os.path.join(a.outdir, 'layout-%s.env' % a.board), 'w') as f:
        f.write('EMMC_SECTORS=%d\n' % a.emmc)
        f.write('BOOT_START=%d\nBOOT_SECTORS=%d\nBOOT_BYTES=%d\n' % (b['start'], b['end'] - b['start'] + 1, (b['end'] - b['start'] + 1) * SS))
        f.write('ROOTFS_START=%d\nROOTFS_SECTORS=%d\nROOTFS_BYTES=%d\n' % (r['start'], r['end'] - r['start'] + 1, (r['end'] - r['start'] + 1) * SS))
    print(fmt_table(g))


def load_ctx_files(a):
    prim = read(os.path.join(a.gendir, 'gpt-primary.bin'))
    sec = read(os.path.join(a.gendir, 'gpt-secondary.bin'))
    tpl = None
    if a.template and os.path.isfile(a.template):
        tpl = load_gpt(read(a.template), 'primary', a.emmc)
    return prim, sec, tpl


def cmd_verify(a):
    prim, sec, tpl = load_ctx_files(a)
    bi = sparse_raw_size(a.boot_img) if a.boot_img and os.path.isfile(a.boot_img) else None
    ri = sparse_raw_size(a.rootfs_img) if a.rootfs_img and os.path.isfile(a.rootfs_img) else None
    R = run_checks(Ctx(prim, sec, tpl, a.emmc, bi, ri, a.boot_uuid, a.root_uuid, parse_expect(a.expect)))
    sys.exit(1 if print_results(R) else 0)


def cmd_selftest(a):
    """Negative controls: every check must FAIL on a deliberately broken copy; and the generator must
    reproduce both backup dumps byte-for-byte when no entry is changed."""
    tpl = load_gpt(read(a.template), 'primary', a.emmc)
    validate_template(tpl, a.emmc)
    fails = 0

    def expect_fail(label, cid, ctx):
        nonlocal fails
        got = {x[0]: x[1] for x in run_checks(ctx)}
        ok = got.get(cid) == 'FAIL'
        print('%-4s negative control: %s -> check "%s" reports FAIL' % ('OK' if ok else 'FAIL', label, cid))
        fails += not ok

    if a.template_secondary and os.path.isfile(a.template_secondary):
        p0, s0 = build(tpl, [e['raw'] for e in used(tpl)], a.emmc)
        ok = p0 == read(a.template) and s0 == read(a.template_secondary)
        print('%-4s generator reproduces both backup dumps byte-for-byte (identity rebuild)' % ('OK' if ok else 'FAIL'))
        fails += not ok
    else:
        print('SKIP identity rebuild vs backup secondary (BOARD_GPT_TEMPLATE_SECONDARY not given)')

    ents = plan_gptb(tpl, a.emmc, a.boot_uuid, a.root_uuid, a.boot_sectors)
    prim, sec = build(tpl, ents, a.emmc)
    base = run_checks(Ctx(prim, sec, tpl, a.emmc, 1 << 20, 1 << 20, a.boot_uuid, a.root_uuid, {}))
    bad = [x for x in base if x[1] == 'FAIL']
    print('%-4s baseline: generated GPT passes all checks%s' % ('FAIL' if bad else 'OK', ' (%s)' % bad if bad else ''))
    fails += bool(bad)

    def mutate(idx, fn):
        e = [bytearray(x) for x in ents]
        fn(e[idx])
        return [bytes(x) for x in e]

    idx = lambda n: [ent_fields(x)['name'] for x in ents].index(n)

    def setstart(delta):
        def f(b):
            b[32:40] = struct.pack('<Q', struct.unpack('<Q', bytes(b[32:40]))[0] + delta)
        return f

    def setend(delta):
        def f(b):
            b[40:48] = struct.pack('<Q', struct.unpack('<Q', bytes(b[40:48]))[0] + delta)
        return f

    m = mutate(idx('modemst1'), setstart(8))
    p1, s1 = build(tpl, m, a.emmc)
    expect_fail('modemst1 start moved', 'calibration-unchanged', Ctx(p1, s1, tpl, a.emmc, 1, 1, None, None, {}))
    expect_fail('modemst1 start moved', 'preserved-identical', Ctx(p1, s1, tpl, a.emmc, 1, 1, None, None, {}))
    m = mutate(idx('sbl1'), setend(-1))
    p1, s1 = build(tpl, m, a.emmc)
    expect_fail('sbl1 shrunk by 1 sector', 'boot-chain-unchanged', Ctx(p1, s1, tpl, a.emmc, 1, 1, None, None, {}))
    m = mutate(idx('rootfs'), setstart(-ALIGN))
    p1, s1 = build(tpl, m, a.emmc)
    expect_fail('rootfs start moved into persist', 'no-overlap', Ctx(p1, s1, tpl, a.emmc, 1, 1, None, None, {}))
    m = mutate(idx('rootfs'), setend(64))
    p1, s1 = build(tpl, m, a.emmc)
    expect_fail('rootfs end past last usable LBA', 'bounds', Ctx(p1, s1, tpl, a.emmc, 1, 1, None, None, {}))
    m = mutate(idx('boot'), setstart(1))
    p1, s1 = build(tpl, m, a.emmc)
    expect_fail('boot start misaligned', 'alignment', Ctx(p1, s1, tpl, a.emmc, 1, 1, None, None, {}))
    p1 = bytearray(prim)
    p1[SS + 16] ^= 0xff
    expect_fail('primary header CRC corrupted', 'crc-primary', Ctx(bytes(p1), sec, tpl, a.emmc, 1, 1, None, None, {}))
    m = mutate(idx('rootfs'), lambda b: b.__setitem__(slice(56, 58), b'x\0'))
    _, s1 = build(tpl, m, a.emmc)
    expect_fail('secondary differs from primary', 'secondary-matches', Ctx(prim, s1, tpl, a.emmc, 1, 1, None, None, {}))
    boot_sz = (ent_fields(ents[idx('boot')])['end'] - ent_fields(ents[idx('boot')])['start'] + 1) * SS
    expect_fail('boot image 4 KiB larger than partition', 'image-boot', Ctx(prim, sec, tpl, a.emmc, boot_sz + 4096, 1, None, None, {}))
    expect_fail('boot PARTUUID does not match', 'partuuid', Ctx(prim, sec, tpl, a.emmc, 1, 1, '00000000-0000-0000-0000-000000000001', a.root_uuid, {}))
    extra = list(ents) + [mk_entry(BOOT_TYPE, a.boot_uuid, 1, 2, 0, 'rogue')]
    p1, s1 = build(tpl, extra, a.emmc)
    expect_fail('unexpected extra entry', 'only-gptb-changes', Ctx(p1, s1, tpl, a.emmc, 1, 1, None, None, {}))
    sys.exit(1 if fails else 0)


def cmd_diff(a):
    tpl = load_gpt(read(a.template), 'primary', a.emmc)
    prim = read(os.path.join(a.gendir, 'gpt-primary.bin'))
    sec = read(os.path.join(a.gendir, 'gpt-secondary.bin'))
    g = load_gpt(prim, 'primary', a.emmc)
    tn = {e['name']: e for e in used(tpl)}
    gn = {e['name']: e for e in used(g)}
    print('== entries (names and LBAs only) ==')
    for e in used(tpl):
        n = e['name']
        if n in gn and gn[n]['raw'] == e['raw']:
            continue
        if n in gn:
            x = gn[n]
            print('CHANGED %-9s %8d..%-8d (%7d)  ->  %8d..%-8d (%7d)' % (n, e['start'], e['end'], e['end'] - e['start'] + 1,
                                                                        x['start'], x['end'], x['end'] - x['start'] + 1))
        else:
            print('REMOVED %-9s %8d..%-8d (%7d)' % (n, e['start'], e['end'], e['end'] - e['start'] + 1))
    for e in used(g):
        if e['name'] not in tn:
            print('ADDED   %-9s %8d..%-8d (%7d)' % (e['name'], e['start'], e['end'], e['end'] - e['start'] + 1))
    print('unchanged entries: %d of %d template entries' % (sum(1 for e in used(tpl) if e['name'] in gn and gn[e['name']]['raw'] == e['raw']), len(used(tpl))))
    print('slot moves (name: template slot -> new slot):',
          ', '.join('%s %d->%d' % (e['name'], i + 1, [x['name'] for x in used(g)].index(e['name']) + 1)
                    for i, e in enumerate(used(tpl)) if e['name'] in gn and [x['name'] for x in used(g)].index(e['name']) != i) or 'none')
    print('== sector-level diff vs template primary (%d sectors) ==' % (len(prim) // SS))
    tp = read(a.template)
    d = [i for i in range(len(prim) // SS) if prim[i * SS:(i + 1) * SS] != tp[i * SS:(i + 1) * SS]]
    names = {0: 'protective MBR', 1: 'GPT header'}
    for i in d:
        print('  LBA %d differs%s' % (i, ' (%s)' % names[i] if i in names else ' (entry array, slots %d-%d)' % ((i - 2) * 4 + 1, (i - 2) * 4 + 4)))
    print('  identical LBAs: %d of %d; mbr identical: %s' % (len(prim) // SS - len(d), len(prim) // SS, prim[:SS] == tp[:SS]))
    if a.template_secondary and os.path.isfile(a.template_secondary):
        ts = read(a.template_secondary)
        d = [i for i in range(len(sec) // SS) if sec[i * SS:(i + 1) * SS] != ts[i * SS:(i + 1) * SS]]
        base = a.emmc - 33
        print('== sector-level diff vs template secondary (%d sectors, first LBA %d) ==' % (len(sec) // SS, base))
        for i in d:
            print('  LBA %d differs%s' % (base + i, ' (backup GPT header)' if i == len(sec) // SS - 1 else ' (entry array sector %d)' % i))
        print('  identical sectors: %d of %d' % (len(sec) // SS - len(d), len(sec) // SS))
    ph, th = load_gpt(prim, 'primary', a.emmc)['hdr'], tpl['hdr']
    print('== header fields ==')
    for k in ('rev', 'my', 'alt', 'first', 'last', 'guid', 'elba', 'n', 'esz'):
        print('  %-6s %s' % (k, 'same' if ph[k] == th[k] else 'DIFFERENT'))
    print('  ecrc   %s' % ('same' if ph['ecrc'] == th['ecrc'] else 'changed (expected: table content changed)'))


def cmd_offsets(a):
    """Shell-sourceable restore geometry computed from the template (no GUIDs)."""
    tpl = load_gpt(read(a.template), 'primary', a.emmc)
    validate_template(tpl, a.emmc)
    reg = tpl['reg_sectors']
    print('PRIMARY_LBA=0')
    print('PRIMARY_SECTORS=%d' % (2 + reg))
    print('PRIMARY_BYTES=%d' % ((2 + reg) * SS))
    print('PRIMARY_MIN_SECTORS=%d' % (2 + (tpl['hdr']['n'] * ESZ + SS - 1) // SS))
    print('SECONDARY_LBA=%d' % (a.emmc - 1 - reg))
    print('SECONDARY_SECTORS=%d' % (reg + 1))
    print('SECONDARY_BYTES=%d' % ((reg + 1) * SS))
    print('SECONDARY_OFFSET=%d' % ((a.emmc - 1 - reg) * SS))
    print('DISK_BYTES=%d' % (a.emmc * SS))
    for e in used(tpl):
        print('PART %s %d %d' % (e['name'], e['start'], e['end'] - e['start'] + 1))


def cmd_sparse_size(a):
    print(sparse_raw_size(a.file))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sp = ap.add_subparsers(dest='cmd', required=True)

    def common(p, outdir=False):
        p.add_argument('--emmc', type=int, required=True)
        p.add_argument('--template')
        p.add_argument('--template-secondary', dest='template_secondary')
        p.add_argument('--boot-uuid', dest='boot_uuid')
        p.add_argument('--root-uuid', dest='root_uuid')
        p.add_argument('--gendir')
        p.add_argument('--boot-sectors', dest='boot_sectors', type=int, default=BOOT_SECTORS_DEFAULT)

    g = sp.add_parser('generate'); common(g); g.add_argument('--outdir', required=True); g.add_argument('--board', required=True)
    g.set_defaults(fn=cmd_generate)
    v = sp.add_parser('verify'); common(v)
    v.add_argument('--boot-img', dest='boot_img'); v.add_argument('--rootfs-img', dest='rootfs_img'); v.add_argument('--expect')
    v.set_defaults(fn=cmd_verify)
    t = sp.add_parser('selftest'); common(t); t.set_defaults(fn=cmd_selftest)
    d = sp.add_parser('diff'); common(d); d.set_defaults(fn=cmd_diff)
    o = sp.add_parser('offsets'); common(o); o.set_defaults(fn=cmd_offsets)
    s = sp.add_parser('sparse-size'); s.add_argument('file'); s.set_defaults(fn=cmd_sparse_size)
    a = ap.parse_args()
    try:
        a.fn(a)
    except GptError as ex:
        print('ERROR: %s' % ex, file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
