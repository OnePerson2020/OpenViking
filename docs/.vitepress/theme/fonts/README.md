# Documentation fonts

Body and display font stacks match https://blog.openviking.ai/ (2026-10-01).
Latin text uses system Charter / Georgia / Palatino. Chinese text uses
TsangerJinKai02 W04 (400) and W05 (500), with the same serif fallbacks as the blog.
Code retains the existing JetBrains Mono font and license.

The TsangerJinKai02 files are lossless WOFF2 conversions of the blog assets:

- https://blog.openviking.ai/assets/TsangerJinKai02-W04-DXmF_ljq.ttf
- https://blog.openviking.ai/assets/TsangerJinKai02-W05-DUsU6zNm.ttf

All glyphs and font metadata are retained. Serving the files locally avoids
cross-origin font failures and adds no third-party CDN dependency.
Embedded font notice: “Before using this font, you must be authorized by Beijing Tsanger Character Technology Co., Ltd.”

Tsanger fonts are not covered by OpenViking's code license. The upstream
Kami README describes personal use as free and commercial use as requiring
permission from https://tsanger.cn; the blog's font authorization must also
cover documentation distribution.
