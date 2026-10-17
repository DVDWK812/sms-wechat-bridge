# Third-party dependencies and license scope

No third-party source tree, wheel, executable, APK or model weight is bundled in the source distribution. Python dependencies are installed separately by the user. Their licenses are not replaced by any license chosen for this project's own code. Any future binary/portable package must separately collect the exact wheels' licenses, bundled-library notices and model permissions; no such portable binary is provided here.

The original Python/PowerShell modules were reviewed for vendor trees, third-party copyright/license headers and legacy automation imports. No vendored pywechat, pyweixin or wxauto implementation is included. References to those alternatives are not a license to redistribute them. Absence of a header cannot prove ownership: project owners must confirm their rights to the original code before the proposed MIT grant is applied.

## Direct runtime dependencies

| Dependency | Tested version | Upstream license / source |
|---|---|---|
| Python | 3.12 x64 | [PSF license](https://docs.python.org/3/license.html) |
| pywin32 | 312 | [BSD-style core terms](https://github.com/mhammond/pywin32/blob/main/win32/License.txt), PSF metadata and component-specific notices; review the installed wheel |
| psutil | 7.2.2 | [BSD-3-Clause](https://github.com/giampaolo/psutil/blob/master/LICENSE) |
| Pillow | 12.3.0 | [MIT-CMU](https://github.com/python-pillow/Pillow/blob/main/LICENSE), plus wheel codecs' notices |
| NumPy | 2.3.5 | [BSD-3-Clause](https://github.com/numpy/numpy/blob/main/LICENSE.txt), plus bundled runtime/BLAS notices |
| PaddleOCR | 3.7.0 | [Apache-2.0](https://github.com/PaddlePaddle/PaddleOCR/blob/main/LICENSE) |
| PaddlePaddle | 3.3.1 | [Apache-2.0](https://github.com/PaddlePaddle/Paddle/blob/develop/LICENSE), plus bundled native libraries |
| PaddleX | 3.7.2 | [Apache-2.0](https://github.com/PaddlePaddle/PaddleX/blob/develop/LICENSE) |

The source-only MIT proposal does not relicense Apache, PSF, BSD or MIT-CMU dependencies. These are separately installed components. Review attribution and notice obligations when distributing any combined artifacts; this audit is not a blanket clearance for arbitrary downstream bundles.

`requirements/sender-lock.txt` records the validated Windows dependency snapshot, including transitive packages. The generated [dependency inventory](docs/DEPENDENCY_LICENSES.json) records installed package versions, license expressions/classifiers and upstream project links without copying license bodies. Metadata can be incomplete, and bundled native components can have additional licenses. Treat missing/ambiguous metadata as requiring review before binary redistribution. Only dependency metadata, not their code or data, is published.

In particular, the installed `python-bidi` package reports LGPL licensing, and `crc32c` reports LGPL/GPL-related metadata. They are separately installed Paddle transitive dependencies, not copied into this source release. A combined executable or bundled Python environment needs a separate license/notice/source-offer review of these components and their exact distributions. This is why no portable executable or dependency bundle is published. Incomplete metadata for colorama, safetensors and wcwidth is supplemented by their license classifiers in the inventory; do not silently treat an unspecified field as MIT.

## External applications and model downloads

- [SmsForwarder](https://github.com/pppscn/SmsForwarder): separately obtained Android app. Its APK and source are not distributed here. Review its license and Android permissions at the upstream project.
- [Tailscale](https://tailscale.com/terms): installed and authenticated separately. Client component licenses and service terms remain upstream's; no Tailscale executable is bundled.
- WeChat: proprietary software, not bundled or modified. Users remain responsible for applicable terms and any automation restrictions. No injection, memory modification, database decryption or private protocol is used.
- Paddle `PP-OCRv5_mobile_det` and `PP-OCRv5_mobile_rec`: no weights or model cache included. The explicit preparation command fetches upstream models. Review the exact model documentation and notices before downloading/redistributing; source repository licensing alone does not establish rights for all training data or every model artifact.
- GitHub CLI is a local publication tool, not part of the source ZIP or runtime dependency set.

## Own-code licensing status

The owner explicitly confirmed ownership/permission and approved MIT for original project code on 2026-10-17. See `LICENSE`. This grant excludes third-party dependencies, applications and models. Public visibility does not extend the project's MIT grant to third-party applications, native libraries, transitive dependencies or model weights.
