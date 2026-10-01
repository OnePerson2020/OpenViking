# Documentation fonts

Chinese body and heading text uses TsangerJinKai02 W04 (400) and W05 (500),
matching https://blog.openviking.ai/ (2026-10-01).
English text, inline code, and code blocks use Maple Mono NF CN v7.9.

Maple's Latin-only CSS face comes first in the body stack. Its unicode-range
allows Latin letters, numbers, and punctuation; Chinese falls through to
Tsanger. Code uses the full Maple face, including Chinese and Nerd Font icons.
Both faces share the same WOFF2 URL, so this does not duplicate downloads.
All glyphs and font metadata are retained in the lossless WOFF2 conversions.

## Maple Mono NF CN

Source: https://github.com/subframe7536/maple-font/releases/tag/v7.9
Archive: MapleMono-NF-CN.zip (checked against the release SHA-256).
Regular, SemiBold, and Italic cover body and code styles. The upstream license
is included in maple-mono-LICENSE.txt. Maple is licensed under SIL OFL 1.1.

## TsangerJinKai02

Source assets:

- https://blog.openviking.ai/assets/TsangerJinKai02-W04-DXmF_ljq.ttf
- https://blog.openviking.ai/assets/TsangerJinKai02-W05-DUsU6zNm.ttf

Serving the files locally avoids cross-origin font failures and adds no
third-party CDN dependency.

Embedded font notice: “Before using this font, you must be authorized by
Beijing Tsanger Character Technology Co., Ltd.”

Tsanger fonts are not covered by OpenViking's code license. The upstream
Kami README describes personal use as free and commercial use as requiring
permission from https://tsanger.cn; the blog's font authorization must also
cover documentation distribution.
