#!/bin/sh
# SPDX-License-Identifier: MIT
# Shared ownership / permission detector. Source this file; do not execute it. Needs no root.
#
# Policy (FLASH-PLAN.md section 9): everything the build creates is root:root. Nothing may have uid or gid >= 1000
# outside /home/$DEFAULT_USER (uid/gid 65534 nobody/nogroup is NOT exempt: it is >= 1000 and not expected in the image),
# and nothing under /etc, /usr, /boot may be group- or world-writable (symlinks excepted: their mode is meaningless).
#
#   own_scan [PREFIX]        reads `tar --numeric-owner -tv` output on stdin, prints one VIOLATION line per finding,
#                            returns 1 if there is any. PREFIX is prepended to every path (for a mounted /boot).
#   own_scan_tar FILE        same for a .tgz/.tar (uses --numeric-owner; never trust a listing without it)
#   own_scan_dir DIR [PREFIX] same for a directory tree (needs read access to every directory: run as root for a rootfs)
#   own_selftest             negative and positive controls on tiny synthetic tars (rootless: uses tar --owner/--mode)
own_scan() {
    awk -v prefix="${1:-}" -v user="${DEFAULT_USER:-openstick}" '
    BEGIN { bad = 0 }
    {
        p = $1; o = $2
        name = $6; for (i = 7; i <= NF; i++) name = name " " $i
        sub(/ -> .*$/, "", name); sub(/ link to .*$/, "", name)
        sub(/^\.\//, "/", name); if (name == ".") name = "/"; if (name !~ /^\//) name = "/" name
        name = prefix name; sub(/\/+$/, "", name); if (name == "") name = "/"
        if (o !~ /^[0-9]+\/[0-9]+$/) { print "VIOLATION non-numeric owner (listing made without --numeric-owner?): " $0; bad = 1; next }
        split(o, id, "/")
        home = "/home/" user
        if ((id[1] >= 1000 || id[2] >= 1000) && name != home && index(name, home "/") != 1) {
            print "VIOLATION owner " o " outside " home ": " name; bad = 1
        }
        if (substr(p, 1, 1) != "l" && name ~ /^\/(etc|usr|boot)(\/|$)/ && (substr(p, 6, 1) == "w" || substr(p, 9, 1) == "w")) {
            print "VIOLATION group/world-writable " p ": " name; bad = 1
        }
    }
    END { exit bad }'
}
own_scan_tar() { tar --numeric-owner -tvf "$1" | own_scan "${2:-}"; }
own_scan_dir() { tar --numeric-owner -cf - -C "$1" . | tar --numeric-owner -tvf - | own_scan "${2:-}"; }

own_selftest() {
    _d=$(mktemp -d "${TMPDIR:-/tmp}/own-selftest.XXXXXX") || return 1
    _rc=0
    mkdir -p "$_d/t/etc" "$_d/t/usr/bin" "$_d/t/boot" "$_d/t/home/openstick" "$_d/t/home/openstickx"
    chmod 755 "$_d/t" "$_d/t/etc" "$_d/t/usr" "$_d/t/usr/bin" "$_d/t/boot" "$_d/t/home" "$_d/t/home/openstick" "$_d/t/home/openstickx"
    : > "$_d/t/etc/a"; : > "$_d/t/usr/bin/b"; : > "$_d/t/boot/c"; : > "$_d/t/home/openstick/h"; : > "$_d/t/home/openstickx/h"
    chmod 644 "$_d/t/etc/a" "$_d/t/usr/bin/b" "$_d/t/boot/c" "$_d/t/home/openstick/h" "$_d/t/home/openstickx/h"
    # base: root:root everywhere except the exempt home (second archive appended)
    mk() { # name, extra tar flags for the etc/a member only (member replaced: appended later wins on extraction, listing shows both)
        rm -f "$_d/$1.tar"
        tar --numeric-owner --owner=0 --group=0 -cf "$_d/$1.tar" -C "$_d/t" ./etc ./usr ./boot
        tar --numeric-owner --owner=1000 --group=1000 -rf "$_d/$1.tar" -C "$_d/t" ./home/openstick
    }
    expect() { # name, PASS|FAIL
        if DEFAULT_USER=openstick own_scan_tar "$_d/$1.tar" >"$_d/$1.out" 2>&1; then got=PASS; else got=FAIL; fi
        if [ "$got" = "$2" ]; then echo "OK   selftest $1: $got as expected"; else echo "FAIL selftest $1: got $got, expected $2"; cat "$_d/$1.out"; _rc=1; fi
    }
    mk clean; expect clean PASS                                           # positive control (incl. exempt /home/openstick)
    mk neg-uid;  tar --numeric-owner --owner=1000 --group=0 -rf "$_d/neg-uid.tar" -C "$_d/t" ./etc/a;  expect neg-uid FAIL
    mk neg-gid;  tar --numeric-owner --owner=0 --group=1000 -rf "$_d/neg-gid.tar" -C "$_d/t" ./etc/a;  expect neg-gid FAIL
    mk neg-nobody; tar --numeric-owner --owner=65534 --group=65534 -rf "$_d/neg-nobody.tar" -C "$_d/t" ./usr/bin/b; expect neg-nobody FAIL
    mk neg-gw;   tar --numeric-owner --owner=0 --group=0 --mode=g+w -rf "$_d/neg-gw.tar" -C "$_d/t" ./usr/bin/b; expect neg-gw FAIL
    mk neg-ow;   tar --numeric-owner --owner=0 --group=0 --mode=o+w -rf "$_d/neg-ow.tar" -C "$_d/t" ./boot/c;   expect neg-ow FAIL
    mk neg-prefix; tar --numeric-owner --owner=1000 --group=1000 -rf "$_d/neg-prefix.tar" -C "$_d/t" ./home/openstickx; expect neg-prefix FAIL
    # a listing made WITHOUT --numeric-owner must never pass silently
    tar -cf "$_d/names.tar" -C "$_d/t" ./etc      # plain tar: stores user/group names
    if tar -tvf "$_d/names.tar" | own_scan >"$_d/names.out" 2>&1; then echo "FAIL selftest names: non-numeric listing passed"; _rc=1; else echo "OK   selftest names: non-numeric listing rejected"; fi
    rm -rf --one-file-system -- "${_d:?}"
    return $_rc
}
