# EASY-MODE-4-SUMMARY (round 9)

ROUND8-HEAD = 2390d64 (verificat la start). HEAD final `installer` = 8e50f79 (wt-V 3c19e25, apoi wt-W 8e50f79, fast-forward).
fix-round-9.patch = `git diff 2390d64..installer` fara notes/, INCREMENTAL. sha256 `5fc55bc21f01846df42b0f7e5e16c84a0a480f9828a29fab62dc98c3179eb6ab` (1850 linii).
Suita completa `tests/run-installer-tests.sh` pe 8e50f79: **TOTAL PASS=48 FAIL=0 SKIP=0**. Nicio actiune pe hardware, fara sudo, fara retea.
Nu am atins: config/*, .gitignore, ROLLBACK.md, tests/verify-rollback-plan.sh, modulele guardate (flash.py, verify_restore.py, edl_runner.py, imageset.py, store.py). Fara subagenti.

## Ce scrie acum `openstick easy` (oglinda a tools/pathA/pathA-flash.sh, fwlib.sh si flash-board.sh)
Ordine: gpt secondary, gpt primary, tz, rpm, sbl1, hyp, aboot, boot (composite), userdata, partitii pastrate, verificare finala read-only. Fara `final`, fara `edl reset`.
Sectoare (verificate prin test contra scripturilor, nu din lista ta): gptS SEC_LBA x33, gptP 0 x34, sbl1 262144 x1024, rpm 268288 x1024, tz 270336 x2048, hyp 272384 x1024, aboot 264192 x2048, boot 396384 x32768.
- `--fw-pkg DIR` obligatoriu (fara el: "Nothing was written"). Verificat inainte de orice scriere: SHA256SUMS (cai nesigure, duplicate, simlink-uri refuzate), dimensiuni exacte, in afara repo-ului, nu simlink.
- Manifest nou, `easy-fw-manifest.env` (BOARD, EMMC_SECTORS, LAYOUT_ID), listat in SHA256SUMS. **Format inventat de mine** (pachetul Path A existent nu il are): trebuie adaugat in pachetul tau privat.
- GPT-T: sursa de adevar = cel derivat din backup (`_layout_from_backup`); GPT-ul setului si cel din pachet trebuie byte-identice, altfel STOP.
- LBA/dimensiune firmware din GPT-ul backup = constantele Path A, `LBA(tzbak) == LBA(tz)+1024`, altfel STOP. tzbak e in `ABSORBED_NAMES` (separat de `WRITTEN_NAMES`). Hash-ul stock al regiunii tz de 2048 sectoare = tz+tzbak din backup.
- Clasificare pre-scriere a 7 regiuni (stock/new/UNKNOWN) cu hash-uri din backup-ul verificat. Orice != stock opreste ("Nothing was written", restore-ul ghidat pentru lant partial, fara reluare).
- tz guard: `_check_tz_range` separat; `WRITTEN_NAMES` extins nu il slabeste. sbl1/rpm/hyp/aboot (scrise) isi pastreaza LBA-ul.
- Composite: lk2nd pin d82f3535972869deb2f963ac33a33ea2cd01cbf1cbd661a4bcc23f491fbcbfde, 524288 B, boot.img la 0x80000, capacitate 0x1000000; refuza daca depaseste; boot.img singur la offset 0 nu se scrie niciodata. **Sursa `out/step1/lk2nd-padded-512KiB.img` NU exista in acest worktree** (nici in builder); testele folosesc un lk2nd sintetic cu hash inlocuit doar in launcherul de test. Pe hardware real trebuie pus acolo fisierul cu hash-ul fixat (sau `LK2ND_IMG`; env-ul pentru hash NU a fost portat, pin-ul ramane fix).
- Evidenta si registry inainte de prima scriere: `flash["firmware"]` = ordinea fazelor, hash-uri noi/stock, hash composite; evidence pathA/{fw,gpt,rollback} cu fw nou din pachet si stock din backup. Doar hash-uri in registry, blobs nu.
- Loader: mecanismul existent (edl_runner, PINNED_LOADER_SHA256); test ca e egal cu LDR_SHA din fwlib.sh (959439aa..., valoare completa). Nicio a doua cale.
- `_edl` accepta doar rs/ws/rf si refuza orice argument cu `reset`; `_phase` refuza `final`/`reset`.
- Final cold-boot: "unplug USB, wait 30 s, plug in WITHOUT the button, then ssh". REENTER_EDL_AFTER_BACKUP ramane True.

## Unde difera `_put` (nou) de `_write_and_check` (vechi)
`_put` (gpt, firmware, composite): pas `--skipwrite` mai intai, apoi scriere, apoi citire INTEGRALA si compara sha256, oprire la prima nepotrivire (ca put() din fwlib.sh). `_write_and_check` ramane doar pentru userdata (fara dry pass, citeste doar primul/ultimul MiB) si partitiile pastrate.

## Offline: `openstick easy --offline [--fw-pkg DIR] [--image-set DIR]`
Fara stick, fara EDL, fara modificari in store. Verifica backup (din store), GPT/layout, pachet, set, composite; tipareste scrierile. Ce cere stick: `HARDWARE_REQUIRED`. Fara backup/pachet/set: "NOT VERIFIED"/HARDWARE_REQUIRED, exit 0, nimic inventat.

## Evidenta pe hardware (raportata de owner, NEVERIFICATA din repo)
2026-11-13: cele 7 faze Path A scrise in apeluri EDL separate ale unei sesiuni, fara cold boot intre ele, 7/7 PASS (log owner). Re-intrarea nu a fost scoasa in aceasta runda.

## Teste noi (checks) si ce dovedeste fiecare mutatie
- easy-fw-chain 678: ordine exacta, dry pass inainte de fiecare scriere, fara `--lun`, `--sectorsize=512`, citire integrala, fara reset/final, composite, evidenta, final check dupa userdata, constante identice cu scripturile (LBA, ordine, loader, pin lk2nd), scut SIGINT. Mutatii: ordine gresita, boot singur, scut/guard scos, oracole alterate.
- easy-fw-guards 56: pachet lipsa/alterat/dimensiune/SHA256SUMS prost/simlink/in repo, GPT pachet != derivat, EMMC/board gresit, regiune UNKNOWN/new, hyp mutat, composite prea mare + limita exacta, lk2nd lipsa/pin/dimensiune, **mutatie care strica `_check_tz_range`** (prinsa), manifest. Fiecare cu mutatie de produs sau de checker.
- easy-fw-failure 47: evidenta inainte de prima scriere, read-back mismatch la rpm (se opreste, mutatie `no-readback-compare` prinsa), restore dupa intrerupere intre faze.
- easy-fw-sigint-a/b 25+25: Ctrl-C in fiecare din cele 8 faze de scriere (gpt x2, tz, rpm, sbl1, hyp, aboot, boot), mutatie scut oprit.
- easy-offline 103: nu scrie nimic, nu apeleaza edl, store identic, HARDWARE_REQUIRED, fara backup; mutatii: offline care atinge store/edl, ordine gresita.
- Actualizate (asteptat): fake board are acum LBA-urile reale ale firmware-ului, plan-guards (limita boot = capacitate - 0x80000), restore-after-* (9 scrieri, 10 faze), identity/happy.

## BLOCAJ pentru decizia ta (nu l-am reparat: ar cere editarea `verify_restore.py`, comanda guardata)
Daca scrierea `tz` (prima faza de firmware) esueaza, discul ramane GPT-T cu tot firmware-ul stock. Restore-ul ghidat refuza: "live firmware is entirely stock; expected Path A state for rollback" (pinuit ca KNOWN LIMIT in easy-fw-failure). Easy refuza un stick deja GPT-T ("restore first"), deci nu exista cale automata. Esecurile la rpm/sbl1/boot se restaureaza corect (testat). Optiuni: sa accepte restore-ul starea "GPT-T + tot stock", sau procedura manuala documentata (pathA-rollback.sh gpt).

## Limite
- Doar testat pe placa falsa (shim edl); nimic pe hardware. Pachetul real trebuie completat cu `easy-fw-manifest.env`.
- Compozitul refuza boot.img care nu e multiplu de 512 B (nu pad-uiesc).
- `_check_stock_signature` cere acum LBA-urile Path A ale firmware-ului: alta placa/layout e refuzata.
