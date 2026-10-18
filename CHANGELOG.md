# Changelog

## 2026-10-18 — GUI transfers are bare pipes; Ctrl-X cancels host-side

**Problem:** (1) The GUI menu/keybind transfers were inconsistent — single
XMODEM was a bare pipe, but the YMODEM batch items ran the full composite
(arming, drive/user targeting, exists/free-space checks, verify, ZMD
cold-boot ride-out). (2) Ctrl-X during a transfer only reached the board;
the host-side protocol loop had no cancel, so after the board-side program
died the bridge sat in its retry/overall timeouts (up to 10 minutes on
YMODEM receive) with a frozen screen — and an exception in the unprotected
receive paths or the GUI key handler could kill the whole bridge process.

**Fix:** (1) All four GUI transfer entries (Send, Receive, Send Batch,
Receive Batch) are now bare protocol pipes: pick file(s), move bytes, toast
the outcome. The board side must be armed by the user (`XM R/S`, `ZMD R`,
`ZMD SB`) — same contract for all. No checks, no constraints; the MCP tools
keep their composite behavior. (2) Ctrl-X during any transfer now cancels on
the host side too: `request_cancel()` sets a flag every protocol loop
consults (via `_xq_get`, which reads as CAN), and sends `CAN CAN` so a
still-alive XM/ZMD aborts. Cancel exits promptly with "cancelled by user",
nudge the prompt back, and skip the ZMD settle wait. `xmodem_receive`/
`ymodem_receive` gained the `except` clause the send paths already had, all
four async wrappers guarantee the callback fires, and the GUI key/paste
handlers no longer let a serial write error unwind the event loop.

## 2026-10-07 — Post-ZMD "-- Hit a key" pause released automatically

**Problem:** on the RC2014 Zed Mini (ZPM3), ZMD 1.50's exit stops at a
"-- Hit a key" prompt before returning to the OS. `_settle_after_zmd` waited
passively for a CP/M prompt, so the console sat dead until a human pressed
something (or the 45 s timeout gave up).

**Fix:** `_settle_after_zmd` now watches for "Hit a key" in the incoming
text (or on the last screen line) and sends ONE CR to release it, then
continues waiting for the prompt. The keypress is gated on the text actually
appearing - a ZMD build without the pause is never poked, so a CR can't land
in a boot loader or an innocent shell. Bounded at 3 presses, 2 s apart.
Verified live on the Zed Mini (COM4): upload + download of a 29-byte file
both returned to a working `A1>` prompt with no manual intervention; bridge
log shows "ZMD paused at 'Hit a key'; pressing CR to release it" followed
by "Post-ZMD settle complete". Tests: `TestSettleAfterZmd` covers
press-on-pause, no-press-without-pause, and silent-board timeout.

## 2026-10-03 — YMODEM download: single-file batches use `zmd sb`

**Problem:** `download_files` / `rc2014_download_files` with exactly one file
failed with "sender cancelled transfer" and zero files received. Root cause:
`ZMD S <one-file>` runs ZMD's single-file XMODEM-1K mode, which sends no
block-0 header - the receiver was waiting for a YMODEM header that never
comes. Verified live on the board: `ZMD SB <file>` prints "1k Batch enabled"
and sends a proper block-0 header even for one file.

**Fix:** `_arm_zmd` now arms sends with the `SB` verb instead of `S`, so
single- and multi-file downloads take the identical batch path (header →
one final 'C' → CAN CAN → cold start → settle → verify). Receive-side
(`ZMD R`) unchanged.

## 2026-10-03 — YMODEM download: one final 'C' ends ZMD's send-batch

**Problem:** the previous end-of-batch fix (never poke after the last file)
was wrong against real ZMD 1.50 S: with its file list exhausted ZMD does
NOT exit - it stays armed at "Your file(s) now ready to download" until its
own ~45-60s no-input timeout fires, so the next bridge operation typed into
a still-armed ZMD.

**Fix:** after the final expected file's EOT-ACK, `ymodem_receive` now
sends exactly ONE final 'C' (~0.5s later, one-shot, not periodic). ZMD
answers it immediately with CAN CAN + "-- File transfer aborted" and
cold-starts - the clean batch end, which `_settle_after_zmd` rides out. A
null block 0 from a better-behaved sender (ACKed) or ~3s of silence are
also clean ends. CAN CAN before the expected count remains an error.

## 2026-10-03 — YMODEM download end-of-batch fix (ZMD CAN CAN)

**Problem:** after the previous fixes, batch downloads transferred every
file byte-perfectly but still returned `{"ok": false, "error": "sender
cancelled transfer"}`. Root cause: after the final file's EOT-ACK,
`ymodem_receive` poked 'C' to solicit the next header - and ZMD 1.50's
send-batch, its file list exhausted, answers that solicitation with
CAN CAN + "-- File transfer aborted" and cold-starts the board. It never
sends the terminating null header in this flow (batch internals live in
ZMDSUBS.REL, no source - this is the observed protocol contract).

**Fix:** `ymodem_receive(..., expected_files=None)` - once the expected
count has arrived, the receiver stops poking 'C' entirely, waits ~3s for a
possible stray null block 0 (ACKing it if one comes), and treats CAN CAN in
that state as the clean batch end it is. CAN CAN before the expected count
is reached remains an error. `download_files` passes
`expected_files=len(names)`; regression tests cover all three cases.

## 2026-10-03 — YMODEM fixes from live testing (ZMD 1.50 quirks)

**Problem:** live runs against the SC126 (ZPM3, ZMD 1.50) surfaced four
issues in the YMODEM batch path:

- Every download failed: after the block-0 header ACK, ZMD opens the file
  and misses a back-to-back 'C' (the race its own changelog describes), so
  data never started and ZMD aborted with CAN CAN ~45 s later.
- EVERY ZMD exit (batch end, abort, manual ^X ^X) cold-starts this board -
  RomWBW banner + autoboot, ~11 s. The post-transfer CR nudge landed in the
  boot loader, and `_arm_zmd`'s XM-inherited banner pattern never matched
  ZMD's real banners ("Receiving on:  Drive A, User 0.", "Your file(s) now
  ready to download", "Abort: ^X pause ^X"), burning the full settle window
  on every arming.
- ZMD advertises 1K-rounded sizes in block 0 (a 561-byte file arrives as
  "1024") because CP/M only knows 128-byte record counts - trimming to the
  advertised size is not byte-exact for ZMD sends.
- The GUI's Transfer menu had no way to start a YMODEM batch.

**Fix:**

- `ymodem_receive` re-pokes 'C' every ~2 s after accepting a header until
  the first data block (or EOT for a 0-byte file) arrives.
- New `_settle_after_zmd()`: passive wait for a fresh prompt after ZMD
  exits, detecting the cold start from boot markers and waiting out the
  autoboot (~45 s cap); quick return (~2.5 s) when a board's ZMD just
  returns to the prompt. Both `ymodem_send` and `ymodem_receive` use it
  (only when an OS was running); the CR nudge is gone from the YMODEM path.
- `_arm_zmd` recognizes ZMD's real banners and treats "Abort:" as the
  armed-commitment line.
- `ymodem_receive`/`download_files`/the MCP tool gained `strip_padding`
  (default True, same convention as the XMODEM path): trim to the
  advertised size, then strip trailing 0x1A. Byte-exact binary wants
  `strip_padding=false`; CP/M record granularity still applies.
- GUI: "Send Files (YMODEM Batch)... (F10)" and "Receive Files (YMODEM
  Batch)... (F11)" driving the full `upload_files`/`download_files`
  composites, with multi-select/directory dialogs (zenity/kdialog/tkinter)
  and on-screen-prompt fallbacks; the status badge now shows the protocol.

## 2026-10-03 — YMODEM batch transfers (ZMD)

**Problem:** moving several files meant one XM arming per file (~12 s
overhead each), uploads were stuck at 128-byte blocks, and XMODEM carries
no file length - so downloads always had CP/M 128-byte record padding
ambiguity (a binary file ending in 0x1A could not survive a download
byte-exactly).

**Fix:** YMODEM send/receive with batch support alongside the untouched
XMODEM path, driving RomWBW's ZMD 1.50 on the board (`zmd r` auto-detects
a batch from the block-0 headers; `zmd s <names>` sends one):

- `SerialLink.ymodem_send` / `ymodem_receive` (plus async wrappers):
  block-0 headers carry `NAME\0<decimal-size>\0`, data blocks are 1K
  CRC16 numbered from 1 per file, first EOT is NAKed then ACKed, and an
  all-NUL block 0 terminates the batch. The receiver trims each file to
  its advertised size - exact lengths both ways. Tolerates ZMD's
  single-file send ending without a null header (~5 s grace).
- `_arm_zmd()` mirrors `_arm_xm()`'s banner-vs-prompt arming checks;
  `--zmd-command` overrides the ZMD invocation like `--xm-command`.
- New composites `upload_files` / `download_files` and MCP tools
  `rc2014_upload_files` / `rc2014_download_files`: all existence checks
  before arming, one arming per batch, board switched to the target
  drive/user and restored afterwards, per-file sha256 in the result.

## 2026-09-10 — CP/NET file push MCP tool

**Problem:** moving files PC → board meant XMODEM (~0.9 KB/s + ~12 s
per-file overhead, and the protocol hogs the console). A first cut of a
CP/NET push existed locally, but with hardcoded machine-specific values
(host paths, IP, drive letters) and a bug: it captured the raw ZPM3 prompt
- ANSI escapes and all - and typed it back at the board as a "command"
after each COPY, spraying `[1m17:44[m A4[1m[m>` garbage over the console.

**Fix:** new `rc2014_cpnet_push` MCP tool, fully config-driven:

- Stages the file into the CP/NET share on the host, ensures NDOS3 +
  `NETWORK <map>` on the board, then `COPY <target_du>=<remote>0:<name>`
  pulls it in (~4.7 KB/s, no console hogging). Auto-starts the socket
  server when a launcher is configured.
- All machine specifics moved to ini keys / CLI flags: `cpnet-share`
  (required - the tool refuses to run without it), `cpnet-server-command`,
  `cpnet-port` (31100), `cpnet-tools-du`, `cpnet-network-map` (`K:=C:[0]`,
  from which the remote `K0:` is derived).
- The post-COPY drive-restore now strips ANSI and regex-parses a clean DU
  out of the captured prompt; if it can't parse one it skips the restore
  (and says so) instead of sending a hardcoded drive.
- No redundant console chatter: the board is switched to the target DU
  only when it isn't already there, the restore is sent only when a switch
  actually happened, and COPY uses the bare source form
  (`COPY <remote>0:<name>` - destination defaults to the current DU) once
  the board is known to sit on the target.

## 2026-09-10 — Pixel-stream gate + XMODEM robustness

**Problem:** two unrelated console/transfer hazards on the SC126 (Z180/ZPM3):

- A stray `0xFA` byte on the wire (reset noise, binary file output)
  auto-switched the reader to `pixel_stream` mode and ate the console, on a
  machine that never runs a Mandel renderer.
- RomWBW spontaneously prints `+++ INVALID Z180 OPCODE @... +++` (resident
  code executing undocumented Z80 IX-half ops the Z180 traps). The line
  contains "invalid", so `_arm_xm`'s `XM_ERROR_RE` scan false-tripped on it
  and cancelled XMODEM uploads that were arming fine.

**Fix:**

- **`app.py` / `link.py`** — new `--pixel-stream` / `--no-pixel-stream` flag
  (ini: `pixel-stream`, default on). When off, the reader never
  auto-switches to `pixel_stream` mode; manual pixel view still works.
- **`link.py` — `_arm_xm()`** — strip `INVALID Z180 OPCODE` lines before
  scanning for XM refusals.
- **`link.py` — `_arm_xm()`** — end the arm-settle 0.4 s after XM's
  `To cancel` banner prints instead of always waiting out the full settle.
- **`link.py`** — debug-log per-block write/ack-wait timing on XMODEM sends.

## 2026-08-29 — ZPM3 command dialect for agents

**Problem:** MCP tool docs and examples listed classic CP/M commands (`STAT`,
`DIR A1:`, `ERA`). On ZPM3 those return `?` or fail. Agents kept sending
`STAT A:` despite project-specific rules elsewhere.

**Fix:**

- **`link.py` — `_prepare_cpm_command()`** — when the connected OS is ZPM3
  (auto-detected from the prompt):
  - `STAT <target>` → **`SDZ <target>`** (response includes `translated_to`)
  - bare **`STAT`** → blocked locally with an error (nothing sent to the board)
  - `DIR <user-area>:` (e.g. `DIR F3:`) → **`SDZ …`**
  - `ERA …` → **`ERASE …`** or a two-step hint (`A1:` then `ERASE file`)
- **`link.py` — `_drive_free_bytes()`** — upload paths use **`SDZ`** on ZPM3
  instead of trying `STAT` first.
- **`mcp_server.py`** — server instructions, `rc2014_run_command` examples,
  `rc2014://docs/cpm-guide`, and `rc2014_scan_drives` text lead with **ZPM3 /
  SDZ**, not `STAT`.
- **`.agents/skills/rc2014bridge_api/SKILL.md`** — same guidance for agents.

**Tests:** `tests/test_run_command.py` — `TestZpm3CommandDialect`,
`TestPrepareCpmCommand`.

**ZPM3 quick reference:**

| Do not use | Use instead |
|------------|-------------|
| `STAT` / `STAT A:` | `SDZ A:` |
| `STAT A1:FILE.COM` | `SDZ A1:FILE.COM` |
| `DIR A1:` | `SDZ A1:` |
| `ERA FILE.COM` | `A1:` then `ERASE FILE.COM` |

Restart the bridge after pulling this change so Cursor reloads MCP tool schemas
and instructions.
