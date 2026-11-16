# WS-C Phase 1 notes

## Findings handled

| Finding | Status | Evidence |
|---|---|---|
| F-06 | VERIFIED-WITH-SHIMS | `seed-apply-transaction-and-best-effort-tools`; `fake-root-apply-idempotency-and-secret-safe-logs`; `service-order-and-builder-installation` |
| F-07 | VERIFIED-WITH-SHIMS | `seed-profile-policy`; `seed-apply-transaction-and-best-effort-tools`; `service-order-and-builder-installation`. `scripts/setup.sh` now writes `/etc/openstick/image-profile` immediately after creating `/etc/openstick`. |
| F-09 | VERIFIED-WITH-SHIMS | `seed-network-values-base64-roundtrip`; `seed-shell-network-values-roundtrip-and-keyfile-escaping`; `configure-hashes-and-private-mode` |
| F-12 seed registry | VERIFIED-WITH-SHIMS | `debugfs-copy-injection-and-readback`; `seed-push-refuses-unavailable-ssh` |
| F-15 | VERIFIED-WITH-SHIMS | `seed-apply-transaction-and-best-effort-tools`; `seed-push-refuses-unavailable-ssh` |

F-09 now preserves leading and trailing spaces entered for Wi-Fi SSID, Wi-Fi PSK, and APN. The captured seed still stores these values in base64 fields. The shell-side trailing-newline rejection is covered by the network keyfile round-trip case.

## Tests

`python3 tests/verify-installer-m3.py .`: **15 PASS, 0 FAIL**. Case names:

- `seed-parser-validation-and-newline-rejection`
- `seed-network-values-base64-roundtrip`
- `seed-profile-policy`
- `configure-hashes-and-private-mode`
- `production-ssh-key-gate-g4`
- `fake-root-apply-idempotency-and-secret-safe-logs`
- `seed-apply-transaction-and-best-effort-tools`
- `seed-shell-network-values-roundtrip-and-keyfile-escaping`
- `invalid-seed-does-not-block-boot`
- `secret-redaction-in-exceptions-registry-and-events`
- `debugfs-copy-injection-and-readback`
- `corrupt-image-rejected-and-cleanup`
- `interrupted-injection-staging-cleanup`
- `seed-push-refuses-unavailable-ssh`
- `service-order-and-builder-installation`

The complete installer runner used a throwaway `TMPDIR` and `tests/shims/bin` first in `PATH`. Result: **25 PASS, 1 FAIL, 0 SKIP**. The unrelated failing runner case was `m7-wizard`; its output was `verification: incomplete evidence set`. M3 passed 15/15 inside that runner. No M3 or WS-C test failed.

`bash -n configs/apply-seed scripts/setup.sh` passed. `python3 -m py_compile tools/installer/seed.py tools/installer/configure.py tools/installer/cli.py tests/verify-installer-m3.py` passed. `shellcheck` was unavailable.

## Not verified offline

- A real boot cannot verify the unit's ordering against the target's actual systemd and NetworkManager startup, or that every supported image reaches the hook as expected.
- Shims do not verify that NetworkManager accepts the generated keyfiles and the quoted UTF-8 values on the device. This needs a real NetworkManager installation.
- No real host configuration, remote SSH transfer, or target seed application was run; push behavior was tested with SSH/SCP shims.

No hardware, sudo, network, or secret values were used.
