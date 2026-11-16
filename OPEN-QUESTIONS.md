# Open questions and owner decisions

The installer work is verified with shims only unless explicitly stated. No EDL command, sudo, network, device dump, or real production image was used. During one earlier M7 validation, doctor read host sysfs and queried device-node permission metadata; it did not run the EDL client or open, read, or write the device node. The aggregate runner was not rerun after this was identified.

## Owner decisions

1. **GPT-T first install:** Keep the current refusal. The adapter verifies and stages synthetic GPT-T inputs, but bootloader-mode identity, write ordering, and complete boot plus userdata readback have not been validated on hardware. Do not remove the refusal without supervised device acceptance.
2. **Post-GPT identity ranges:** The installer stops after the GPT phase because new `fsg`, `sec`, and `fsc` LBAs have not been established on hardware. Keep this guard until those ranges are measured and validated.
3. **Path A firmware provenance:** The sanitized checkout contains no Path A firmware/GPT/rollback blobs. The wrapper verifies hashes for supplied inputs, but an owner must provide and establish provenance for any future image set.
4. **Prepared base image:** `build` starts from an existing `out/base-bookworm-arm64.tar.gz`; it does not run `10-base.sh`. An owner must prepare and verify this base before a real build.
5. **Image baseline:** The original boot and rootfs images and pinned build inputs are unavailable, so byte-for-byte comparison with the M0 images cannot be done. Keep the report's baseline comparison as unavailable unless those inputs are supplied.
6. **Reset and boot:** The installer does not issue the final EDL reset or claim boot success. An owner must review phase evidence and perform any later reset or boot step explicitly.
7. **End-to-end acceptance:** No successful real-client wizard doctor-then-backup flow has been run. The CLI regression verifies safe refusal when prerequisites are missing, but it does not establish a working EDL session. M4 and M7 remain PARTIAL until non-mock end-to-end evidence exists. The owner should use the read-only checklist in `INSTALLER-REPORT.md`; hardware acceptance is outside this work.

## Recorded behavior to retain

- The registry is a root-level `registry.json` with append-only `events.jsonl`. Registry paths are absolute, so moving a store requires path review.
- `seed push` requires an explicit `USER@HOST`; no default device address is assumed.
- `--i-accept-untested` does not exist. Non-`ufi103s` boards are always refused.
- M3 writes Wi-Fi and APN values using base64 seed fields and NetworkManager keyfile escaping. Real NetworkManager acceptance remains unverified.
- M6 standalone install and restore scripts are explicitly marked UNVALIDATED on hardware and require typed confirmation for writes.
- EDL client and loader resolution is shared by doctor, backup, flash, restore, and package creation. Client calls pass the verified absolute loader and `--memory=emmc`; timeouts interrupt the process before termination and report the cold-boot recovery step.
- Doctor checks USB access by scanning sysfs for an attached EDL device and checking the corresponding node's read/write permission. New regression tests use fixture sysfs and injected access checks. No system files are written by doctor.
- F-22 is closed: the owner-private deny-list stays outside the repository and is not part of installer integration.
