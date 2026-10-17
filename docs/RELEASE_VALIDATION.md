# Publication-copy validation / 发布副本验证

Date: 2026-10-17. Source-only review candidate; forwarding remains OFF. The repository must remain Private until the owner approves public release.

| Check | Result and scope |
|---|---|
| Source isolation | Fresh publication directory and fresh Git history; no production Git/history imported |
| Existing tests | 130 source tests preserved with sanitized fixtures; duplicate-selection expectations updated for the publication safety requirement |
| Publication regression | 131 unit tests PASS, including an additional collapsed-search-result rejection test |
| Native integration | 9 checks PASS using temporary loopback HTTP, databases, real Windows processes/Job Objects and a mocked GUI sender |
| Native coverage | On/off listener health, signed HTTP/rejections/dedup, five switch cycles, crash recovery, uncertain reservations, active-stop behavior, redacted status, owner-crash child cleanup |
| Clean environments | Fresh Python 3.12 x64 receiver and sender venvs installed; `pip check` PASS in both |
| OCR dependencies | PaddleOCR 3.7.0 / PaddlePaddle 3.3.1 / PaddleX 3.7.2 installed; runtime import PASS; no models copied or OCR of real chats performed |
| Lock manifest | Exact fresh sender environment recorded with upstream PyPI wheel SHA-256 hashes; hash-required dependency resolution PASS |
| Scripts/config | Every root PowerShell script parsed; isolated initialization and DPAPI round trip PASS; repeated initialization preserves existing config |
| Actual tray | Temporary OFF tray launched independently, second launch retained same PID, no receiver launched, graceful Exit PASS |
| Login startup | Interactive/limited Task Scheduler definition validation PASS; no login task registered and no actual logoff/login performed |
| Live sending | Zero SMS/WeChat sends during publication; no phone reconfiguration or live acceptance rerun |
| Source license | MIT approved by owner; dependencies remain separately licensed |

The earlier deployment's Windows 11 Pro build 26100 / WeChat 4.1.15.13 / Android 16 live results establish only that deployment's compatibility. Publication-specific changes include isolated paths, data-directory-scoped tray singleton, sanitized test fixtures, Paddle-based CLI inspection and stricter ambiguity refusal. These changes passed automated validation but have not received a new live WeChat acceptance run. Never describe mocks or a Send-key return as independent recipient receipt.

The privacy scan checks publication source, the Git index, reachable history, and the source ZIP. It searches known private identifiers, realistic mobile-number patterns, personal Windows paths, tokens, DPAPI blobs and forbidden artifacts. It does not prove absence of every possible secret; manual file-list/source review remains part of release preparation. GitHub Actions status and the final artifact digest are reported with the repository handoff, avoiding a self-referential commit hash in this file.

No production source/configuration was intentionally changed. No original service was started/stopped. The original tray was already not running, with forwarding disabled, at the start of publication work. Publication tests used independent runtime directories, ports and task names.

Before first real enablement: install local models, pair the actual phone, set a unique recipient, dry-run identity verification, explicitly approve a fresh invalid message and check receipt separately. Real Windows locking, sleep, RDP disconnection, network disruption and login-startup acceptance were not performed during publication. Retain uncertain-send records; do not clear them to force a retry.
