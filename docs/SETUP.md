# Configuration reference

Use Windows PowerShell as the logged-in desktop user. Runtime files are outside the checkout in `%LOCALAPPDATA%\SmsWechatBridgeOSS` or the absolute `SMS_WECHAT_DATA_DIR`. Models use `SMS_OCR_MODEL_DIR` or `paddle-models` under that data directory. Do not move a DPAPI secret between users/computers; initialize a fresh secret and pair manually. Keep the same data-directory environment at Explorer and Task Scheduler startup. For a custom path, set a user environment variable before starting and log in again before testing login startup.

| Local config field | Meaning |
|---|---|
| `SMS_BIND`, `SMS_PORT` | Windows Tailscale IPv4 and TCP port (default 8765) |
| `SMS_ALLOWED_CLIENT_IPS` | Exact phone Tailscale IPv4; never a subnet or public source |
| `SMS_ALLOWED_SENDERS` | Comma-separated exact received numbers, including country code |
| `SMS_ALLOW_ALL_SENDERS` | `0` restricts numbers; `1` explicitly opts into all numbers |
| `SMS_FILTER_MODE` | `contains`, `not_contains`, `all` |
| `SMS_FILTER_KEYWORD` | Literal nonempty keyword, max 128 characters for restricted modes |
| `WECHAT_TARGET`, `WECHAT_TARGET_TYPE` | Unique full name; `friend`, `group`, `chat` (auto) |
| `SMS_DRY_RUN` | Initially `1` |
| `SMS_REAL_SEND_APPROVED`, `WECHAT_TARGET_VERIFIED` | Initially `0`; use tray verification and human confirmation |
| `SecretProtected` | Generated current-user DPAPI data; never commit it |

`AcceptanceComplete` is a legacy direct-listener startup gate, not permission to send or proof of delivery. Prefer the managed tray. Do not edit production flags to bypass its approval scope. The tray's identity proof pins target, type, selection policy and client version; target/filter changes revoke approval.

The OCR adapter pins its tested client version in `app_paths.VERSION`. A new version needs a deliberate compatibility review and fresh identity/invalid-message acceptance, not just changing a version string. Default process/window discovery uses the standard WeChat executable and window class from the tested release; unsupported layouts stop. The Tailscale CLI is discovered under Program Files; an explicit `TAILSCALE_EXE` override may point to an official installed executable.

`prepare_paddle_models.py` explicitly downloads `PP-OCRv5_mobile_det` and `PP-OCRv5_mobile_rec` via PaddleOCR, then stages them under the configured local model root. Existing destination directories are not overwritten. See [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) and upstream model documentation for model licenses. No weights are supplied by this repository. Use an ASCII path if Paddle cannot load a path containing non-ASCII characters.

For manual firewall setup, permit only TCP from the exact phone IP to the exact Windows Tailscale IP/port, scoped to the actual Python interpreter. The helper `configure_firewall.ps1 -WindowsTailIP <IP> -PhoneTailIP <IP> -Port 8765` needs an explicitly elevated shell. Other scripts do not require elevation. Never disable Windows Firewall.

Optional `set_pairing.ps1 -PhoneTailIP <IP>` discovers the local Tailscale IPv4 after checking the specified online Android peer. Stop the tray before any manual pairing/config changes. `set_tray_autostart.ps1 -Mode Status` reads the setting; `-Mode Validate` builds an interactive limited task definition without registration. `Enable`/`Disable` modify only the publication variant's task after an explicit user choice.

Before real enablement, run tests, verify the target in dry-run, and explicitly approve a fresh invalid message with the recipient's consent. Separately check receipt on the destination device. A real phone acceptance must actually use the phone while locked and disconnected from USB; a local mock must never be described as that acceptance.
