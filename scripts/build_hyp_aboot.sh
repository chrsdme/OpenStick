#!/bin/sh
set -e
# Derived from kinsamanka/OpenStick-Builder (MIT). Builds firmware images only; flashes nothing.
. "$(dirname "$0")/common.sh"
cd "$ROOT"

make -C src/qhypstub CROSS_COMPILE=aarch64-linux-gnu-

# patch to reduce mmc speed as some boards have intermittent failures when
# inititalizing the mmc (maybe due to using old/recycled flash chips)
grep -q USE_TARGET_HS200_CAPS src/lk2nd/project/lk1st-msm8916.mk || echo 'DEFINES += USE_TARGET_HS200_CAPS=1' >> src/lk2nd/project/lk1st-msm8916.mk

make -C src/lk2nd LK2ND_BUNDLE_DTB="msm8916-512mb-mtp.dtb" LK2ND_COMPATIBLE="${LK2ND_COMPATIBLE:-yiming,uz801-v3}" \
    TOOLCHAIN_PREFIX=arm-none-eabi- lk1st-msm8916

# test sign
mkdir -p out/fw
src/qtestsign/qtestsign.py hyp src/qhypstub/qhypstub.elf \
    -o out/fw/hyp.mbn
src/qtestsign/qtestsign.py aboot src/lk2nd/build-lk1st-msm8916/emmc_appsboot.mbn \
    -o out/fw/aboot.mbn
