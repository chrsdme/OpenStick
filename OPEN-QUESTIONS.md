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
- The workspace-level `CLAUDE.md` and `AGENTS.md` exist, but this builder tree has no local `CLAUDE.md`, `spec/INSTALLER-SPEC.md`, or `spec/ACCEPTANCE.md`; the round follows the supplied workspace instructions and round plan. Confirm whether these builder-local files should be restored in a future clean copy.

## De intrebat (din rularea pe masina reala, 2026-11-22)

- Q-01 - BAZA: planul cere verificarea patch-ului de runda 10 pe `611686a` (tag `r9-48of48`),
  dar HEAD-ul acestui checkout e `8e50f79` (baza lui Codex). Care e baza corecta pe care se
  aplica patch-ul, si unde se afla `611686a`? Until this is answered, `git apply --check`
  decides nothing. Default recomandat: verifica cu `git cat-file -t 611686a` inainte de orice
  altceva, si nu aplica nimic pana raspunsul vine.

- Q-02 - CALE: planul spune "Aplici pe `~/openstick/builder`", dar aici exista
  `~/openstick/openstick-codex/builder`. Sunt doua clone-uri ale aceluiasi repo, sau sunt doua
  repo-uri diferite? Depinde de raspuns: daca sunt doua clone-uri, unde se aplica patch-ul si
  unde se ruleaza suita.

- Q-03 - REMOTE: repo-ul `github.com/chrsdme/OpenStick.git` exista si e creat, dar nu s-a
  facut niciun push. Este privat? Se poate face push acum, sau a ramas blocat pana la review
  (regula "privat intai" e clara, dar "review-ul" nu e definit ca data)?

- Q-04 - PATCH: in radacina sunt `fix-round-9.patch` si `fix-round-10.patch` (+ `.sha256`).
  Care e patch-ul de runda 10 care trebuie verificat si aplicat - cel cu `.sha256`, sau celalalt?

- Q-05 - PRIVATE FILES: `config/user.env`, `config/local.env`, `config/precommit-deny.txt` sunt
  urmarite de git in acest checkout? Au fost vazute in status la instalare, dar scanul de
  secrete le-a marcat doar dupa nume. Daca sunt urmarite, trebuie scoase inainte de orice push.

- Q-06 - WT-*: worktree-urile `wt-A`..`wt-Y` (registrate in builder sau doar foldere) trebuie
  sterse acum, sau raman pana la T8? Un `git worktree prune` le-ar stinge din evidenta, dar
  daca contin munca nesalvata, se pierd.

## De intrebat (din rularea pe masina reala, 2026-11-22)

- Q-01 - BAZA: planul cere verificarea patch-ului de runda 10 pe `611686a` (tag `r9-48of48`),
  dar HEAD-ul acestui checkout e `8e50f79` (baza lui Codex). Care e baza corecta pe care se
  aplica patch-ul, si unde se afla `611686a`? Until this is answered, `git apply --check`
  decides nothing. Default recomandat: verifica cu `git cat-file -t 611686a` inainte de orice
  altceva, si nu aplica nimic pana raspunsul vine.

- Q-02 - CALE: planul spune "Aplici pe `~/openstick/builder`", dar aici exista
  `~/openstick/openstick-codex/builder`. Sunt doua clone-uri ale aceluiasi repo, sau sunt doua
  repo-uri diferite? Depinde de raspuns: daca sunt doua clone-uri, unde se aplica patch-ul si
  unde se ruleaza suita.

- Q-03 - REMOTE: repo-ul `github.com/chrsdme/OpenStick.git` exista si e creat, dar nu s-a
  facut niciun push. Este privat? Se poate face push acum, sau a ramas blocat pana la review
  (regula "privat intai" e clara, dar "review-ul" nu e definit ca data)?

- Q-04 - PATCH: in radacina sunt `fix-round-9.patch` si `fix-round-10.patch` (+ `.sha256`).
  Care e patch-ul de runda 10 care trebuie verificat si aplicat - cel cu `.sha256`, sau celalalt?

- Q-05 - PRIVATE FILES: `config/user.env`, `config/local.env`, `config/precommit-deny.txt` sunt
  urmarite de git in acest checkout? Au fost vazute in status la instalare, dar scanul de
  secrete le-a marcat doar dupa nume. Daca sunt urmarite, trebuie scoase inainte de orice push.

- Q-06 - WT-*: worktree-urile `wt-A`..`wt-Y` (registrate in builder sau doar foldere) trebuie
  sterse acum, sau raman pana la T8? Un `git worktree prune` le-ar stinge din evidenta, dar
  daca contin munca nesalvata, se pierd.
