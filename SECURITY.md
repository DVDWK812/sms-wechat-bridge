# Security / 安全说明

Never publish `config.json`, DPAPI blobs, secrets, tokens, phone numbers, private device addresses, recipient names, SMS content, screenshots, logs, databases, models or backups. `.gitignore` is a guard, not a substitute for scanning staged files, every reachable commit and release archives.

SmsForwarder's supported HMAC convention signs the timestamp using the shared secret. **It does not cryptographically bind the sender or SMS body.** Do not describe it as full-payload authentication. Tailscale encryption, strict source-IP filtering, tailnet access policy and device security are essential. Do not expose the endpoint publicly, disable timestamp/source checks, or share a tailnet with untrusted devices. Requests expire and uncertain sends remain reserved; this is not an exactly-once delivery guarantee.

The phone's `[org_content]` is passed through the existing SmsForwarder form-template behavior. Test special characters (`&`, `+`, `%`, line breaks and Unicode) with invalid data when changing app versions. Desktop input rejects unsafe control characters and oversized messages. WeChat UI changes or OCR mistakes can cause safe refusal; the tool cannot prove the absence of an unseen duplicate name. Use unique names and manually validate configuration before enabling.

The tray stores secrets protected by current-user DPAPI. That does not protect against malicious software running as that same user or against screenshots of a revealed pairing secret. Do not export credentials into shell history, Git or issue attachments. Private clipboard text is excluded from clipboard history/cloud where supported and cleared only if the program still owns the sequence; a crash can still require manual clipboard inspection.

Do not run multiple releases or an old deployment on the same port. Do not run GUI automation under Session 0. Stop forwarding before changing local configuration, recipient, firewall or network settings. A send already submitted to WeChat cannot be recalled by switching off. Successful Send action is not verified delivery. Unknown outcomes require manual review and must not be automatically retried.

Use only messages you are authorized to forward. Keyword-unrestricted and all-sender modes can expose private unrelated messages. WeChat/SmsForwarder/Tailscale remain third-party products governed by their terms. This project uses desktop UI automation without injection, process-memory modification, chat database decryption or private messaging protocols.

Report suspected vulnerabilities privately to the repository maintainer through an available private GitHub reporting channel. Do not include a working secret, real SMS or personal screenshots in a public issue. If no private channel is configured, request a contact route without disclosing exploit data.
