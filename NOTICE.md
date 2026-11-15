# Attribution

This project is derived from [kinsamanka/OpenStick-Builder](https://github.com/kinsamanka/OpenStick-Builder)
(MIT License, Copyright (c) 2024 GP Orcullo). The original license is kept in `LICENSE`.

Files derived from it (modified): `scripts/20-rootfs.sh`, `scripts/setup.sh`, `scripts/30-gt.sh`,
`scripts/40-images.sh`, `scripts/extract_fw.sh` (GPT part moved to `gen-gpt.sh`), `scripts/build_hyp_aboot.sh`, `configs/*`.
`scripts/msm-firmware-loader.sh` is unmodified (SPDX: MIT).

Device tree sources in `dts/` (`msm8916-ufi.dtsi` modified for the 6.6 tree) taken from [x7780/OpenStick-Builder](https://github.com/x7780/OpenStick-Builder)
keep their `SPDX-License-Identifier: GPL-2.0-only` headers. Precompiled `.dtb` files in `contrib/dtb/`
come from the original Builder.

Fetched at build time (not redistributed here): lk2nd, qhypstub, qtestsign (msm8916-mainline),
gt and libusbgx (linux-usb-gadgets), the postmarketOS msm8916 kernel package, and Qualcomm
firmware from the Dragonboard 410c image (sha256 verified).

Optional third-party UIs are only linked, never copied, because their licenses are unclear or restrictive:
[isShawnHsu/OpenStick-ZU801-WEB](https://github.com/isShawnHsu/OpenStick-ZU801-WEB),
[murdokoja-max/openstick-config](https://github.com/murdokoja-max/openstick-config).
