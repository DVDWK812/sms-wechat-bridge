# Public-release privacy review / 公开前隐私审查

Review date: 2026-10-17. Scope: GitHub repository contents and **all four then-reachable commits** before this publication-documentation update.

- Inspected all **46 tracked file paths per commit**, giving **51 distinct text blobs** after deduplication across the four snapshots. No Git-tracked binary artifacts were found in those snapshots.
- Pattern-reviewed source and history for actual local Windows user paths, previously known private contact identifiers, GitHub tokens, common PEM private-key headers, realistic Chinese mobile numbers, email addresses and Tailscale/private-network addresses.
- All flagged Tailscale-address uses were explained by the standard network range `100.64.0.0/10` in source and synthetic `100.64.0.x` test fixtures, **not the deployment's individual device addresses**.
- The existing CI also runs `scripts/scan_release.py --history`, covering forbidden artifacts, common credential formats, DPAPI-shaped values and private local paths. Earlier publication testing separately examined the distribution ZIP.
- No known personal SMS, conversations, contact names, production endpoints, passwords or tokens were identified in that scoped review. This is a **bounded negative finding, not a proof that arbitrary private information can never occur**.
- GitHub account identity, commit authorship and commit timestamps are inherently visible when the repository is Public; verify your GitHub profile and commit metadata before making it Public.
- This source-only audit did not access the user's Windows production runtime or trigger a live SMS/WeChat send. The publication copy's stricter recipient-ambiguity handling still requires separately authorized testing on a real desktop.
- Never attach actual SMS, QR/pairing secrets, screenshots of private chats or production configuration to issues, releases or CI artifacts. Scan future commits, tags and release archives again before publishing.

The sole intended public outputs are source code, synthetic test fixtures, installation documentation and license notices. Refer to [SECURITY.md](../SECURITY.md) for residual security risks. This audit is descriptive; it does not grant approval to disclose other people's messages.
