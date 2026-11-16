# WS-F: doctor EDL and USB diagnostics

## Findings handled

- H-05: doctor resolves its EDL client through `edl_runner.resolve_client()` and the verified Firehose loader through `edl_runner.resolve_loader()`.
- H-06: doctor scans a caller-supplied sysfs root for `05c6:9008`, derives the USB bus/device node, and checks its read/write access. An attached inaccessible device receives the narrow `uaccess` rule hint; `GROUP=plugdev` is included only when that group exists. No attached device reports INFO and does not fail doctor. Doctor does not write system files.
- H-09: the EDL client, loader, and USB diagnostics now have specific guidance. The unrelated package installation hint remains only for generic host tools.

## Tests added

`tests/verify-installer-doctor.py` passed **10/10**:

- `test_usb_present_with_access_uses_device_node_path`
- `test_usb_present_without_access_fails_with_narrow_udev_hint`
- `test_usb_absent_is_info_and_does_not_fail_doctor`
- `test_usb_sysfs_scan_error_is_a_failure_with_a_specific_hint`
- `test_edl_resolver_prefers_executable_then_script`
- `test_edl_resolver_falls_back_to_path`
- `test_loader_resolver_refuses_a_digest_mismatch`
- `test_doctor_output_uses_shared_resolvers_and_specific_hints`
- `test_doctor_failure_hints_are_specific_for_edl_loader_and_usb`
- `test_plugdev_hint_only_mentions_existing_group`

The initial test-first run failed because the doctor had no fixture sysfs helper or injectable root. After implementation, the doctor fixture suite passed 10/10. `tests/verify-installer-m2.py` passed 10/10. The existing `tests/verify-installer-m7.py` passed 25/25 before the orchestrator directed that no further host sysfs or device-node checks be run.

## Validation and limitations

- `python3 -m py_compile tools/installer/cli.py tests/verify-installer-doctor.py`, `bash -n tests/run-installer-tests.sh`, and `git diff --check` passed.
- The aggregate `tests/run-installer-tests.sh` was not run from this worktree: it invokes the M7 CLI doctor path, which uses host sysfs. This was withheld after the orchestrator's instruction to avoid any further host sysfs or `/dev` checks.
- During the already-run M7 CLI regression, doctor detected an attached Qualcomm 9008 device and checked only its device-node permissions with `os.access`. No EDL command, device open/read/write, or other hardware operation was run. The orchestrator directed that no further such host checks be run. All new USB tests use temporary sysfs and device-root fixtures and injected access callbacks.
- No real board/client compatibility, loader availability, udev permissions, sudo, or network behavior was tested.

## Secret scan

**SCAN-CLEAN.** No secret-looking values were found in files changed by WS-F.
