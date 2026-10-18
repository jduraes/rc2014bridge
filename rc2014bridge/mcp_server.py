"""
Model Context Protocol (MCP) server for the RC2014 bridge - the only control
surface. Serves the stateless streamable HTTP transport (protocol 2026-07-28
and later) at /mcp from one uvicorn instance, bound to 0.0.0.0:8014 by
default. Every request is self-contained - no initialize handshake, no
Mcp-Session-Id, no persistent connection - so there is no legacy SSE
transport to maintain either.

The tool surface is built around rc2014_run_command: one call sends a command,
waits for the CP/M prompt to come back, and returns just that command's output.
The lower-level rc2014_send_text / rc2014_get_screen pair remains for things
that never return to a shell prompt - interactive programs, the HBIOS boot
menu, answering a Y/N question mid-command.
"""

import functools
import json
import logging
import threading
from typing import Optional

import anyio
import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.middleware.cors import CORSMiddleware

logger = logging.getLogger("rc2014bridge.mcp")

SERVER_INSTRUCTIONS = """
You are connected to a Z80 retro computer running RomWBW (HBIOS) with ZSDOS /
CP/M 2.2, over a serial console this bridge owns permanently. A human may be
watching the same session in a terminal window, so what you type is visible.

How to drive it:
- `rc2014_run_command` is the tool to reach for. It sends a command, waits for
  the prompt to return, and gives back only that command's output - no polling,
  no guessing when it finished.
- `rc2014_send_text` + `rc2014_get_screen` are for anything that does NOT
  return to a shell prompt: interactive programs (MBASIC, ED), the HBIOS
  `Boot [H=Help]:` menu, or answering a prompt mid-command. `run_command` will
  time out on those.
- `rc2014_send_keys` sends control characters and is the way to interrupt
  something - `^C` to break out of a program, `^X<PAUSE>^X` to abort a stuck
  XM transfer. It is the one tool allowed to run while something else owns the
  wire.
- `rc2014_upload_file` / `rc2014_download_file` are THE DEFAULT file transfer
  tools - they take host paths and no file content ever crosses MCP.
  `rc2014_upload_files` / `rc2014_download_files` move several files in one
  YMODEM batch (1K blocks, one arming) - prefer them for multi-file work.
  `rc2014_upload` / `rc2014_download` (inline content, base64 for binary) exist
  only for content that lives in the conversation itself. All six drive XM/ZMD
  and verify; do not drive XM or ZMD by hand. IMPORTANT: when ZMD exits, this
  board cold-starts (RomWBW banner + autoboot, ~11s) - expected, not a crash;
  the batch tools wait it out.
- `rc2014_read_text_file` is a cheaper alternative for a quick peek at a text
  file, but not byte-exact (TYPE expands tabs and stops at 0x1A) - use
  `rc2014_download_file` when the exact bytes matter.

What to expect:
- Filenames are CP/M 8.3 uppercase (`MBASIC.COM`, `DEMO.Z80`). Drives are `A:`
  through `P:`; programs are run by bare name, without the `.COM`.
- **ZPM3 dialect (this machine):** do **not** use classic `STAT`, `ERA`, or
  `DIR <user-area>:` — they return `?`. Use **`SDZ`** for listings, drive free
  space, and file checks (`SDZ A1:`, `SDZ A1:VTUNE.COM`). Use **`ERASE`**
  (after `A1:` etc.) instead of `ERA`. The bridge auto-translates `STAT …` and
  `DIR F3:` to `SDZ …` when it can; bare `STAT` is rejected with a hint.
- Only one operation can own the serial wire at a time. A tool returning
  `{"busy": true}` means wait and retry, not that the machine is broken.
- Read `rc2014://docs/hardware-overview` for this specific machine's captured
  configuration rather than assuming a standard build. `rc2014_get_hardware_info`
  returns the same data as JSON.
- Text sent to the board is paced one byte at a time with a 15ms gap, because
  an unpaced burst at 115200 baud overruns the UART while the Z80 is busy.
"""

CPM_GUIDE_DOC = """
# CP/M 2.2 & ZSDOS Quick Reference

## ZPM3 / ZCPR (this connected machine)

ZPM3 does **not** speak classic CP/M `STAT` / `ERA` / `DIR <user>:`.

| Do not use | Use instead |
|------------|-------------|
| `STAT` or `STAT A:` | `SDZ A:` (free space + listing trailer) |
| `STAT A1:FILE.COM` | `SDZ A1:FILE.COM` |
| `DIR A1:` | `SDZ A1:` |
| `ERA FILE.COM` | `A1:` then `ERASE FILE.COM` |

`rc2014_run_command` auto-translates `STAT <target>` → `SDZ <target>` and
blocks bare `STAT` with an error. Prefer typing `SDZ` directly.

## Filenames
CP/M 8.3, uppercase (`MYPROG.COM`, `SRC.Z80`, `README.TXT`). A drive prefix is
`X:` (`B:HELLO.TXT`). Run a program by bare name - `MBASIC`, not `MBASIC.COM`.

## Built-ins and common utilities

On **ZPM3**, use the ZPM3 table above instead of the `STAT`/`ERA` rows here.

- `DIR <drive>:`     List directory contents (ZPM3: use `SDZ`).
- `DIR <file>`       Check whether one file exists (ZPM3: `SDZ <file>`).
- `SDZ <drive>:`     ZPM3 listing + `Free: Nk` trailer (drive/user OK).
- `STAT`             Classic CP/M drive access mode and free space (**not ZPM3**).
- `STAT <file>`      File size and record allocation (**not ZPM3**).
- `TYPE <file>`      Print a text file to the console.
- `ERA <file>`       Erase (ZPM3: use `ERASE` after selecting drive/user).
- `ERASE <file>`     ZPM3 erase (bare 8.3 name in current drive/user).
- `REN <new>=<old>`  Rename.
- `PIP <dest>=<src>` Copy (e.g. `PIP B:=A:MYPROG.COM`).
- `XM R|S <file>`    XMODEM transfer. Prefer the upload_file/download_file
                     tools, which drive this correctly and verify the result.
- `ZMD R` / `ZMD SB <names>`  X/YMODEM batch transfer (SB forces batch mode;
                     plain S with one file silently drops to XMODEM-1K). Prefer
                     the upload_files/download_files tools, which drive it.
- `MBASIC`           Microsoft BASIC-80. `SYSTEM` returns to CP/M.
- `ED <file>`        CP/M line editor (interactive - not for run_command).
- `SUBMIT <file>`    Run a `.SUB` batch file.
- `ASSIGN`           Show or change drive-to-device mappings.
- `REBOOT`           Clean restart back to the RomWBW boot loader.

## The HBIOS boot loader (`Boot [H=Help]:`)

Single keystrokes only - it acts on one key, so sending a word runs its first
letter and then returns a prompt, which looks like success. Use
`rc2014_send_text` with one character, never `rc2014_run_command`.

- `H` - help. Lists `L D R W I V` and `<unit>[.<slice>]` to boot a disk.
- `L` - **list ROM applications**, and the only route to several of them. The
  XModem Flash Updater is *not* on the help menu: it is `X` from this list.
  Confirmed on an SC700 running RomWBW v3.5.0, which offers:
  `M` Monitor, `C` CP/M 2.2, `Z` Z-System, `B` BASIC, `T` Tasty BASIC,
  `F` Forth, `P` Play a Game, `N` Network Boot, `X` XModem Flash Updater,
  `U` User App.
- `D` - device inventory: console devices and disk units with capacities. The
  quickest way to learn which unit number to boot.
- `R` - reboot.
- Booting is by unit number from `D`, optionally `unit.slice` - and it differs
  per machine (unit 2 on one RC2014, unit 4 on an SC700). Never assume.

After booting, call `rc2014_wait_until_ready`: the boot profile keeps running
programs after the prompt appears, and commands sent during that window are lost.

## Things that surprise people
- A `.COM` file is loaded at 0x0100 in the TPA; TPA size limits program size.
- The RAM disk (MD0) survives a *reset* - it is static RAM, and only a real
  power cycle clears it. Don't assume a reset gives a clean slate.
- The ROM disk (MD1) is genuinely read-only; writes to it fail.
- Text files conventionally end with a 0x1A (Ctrl-Z) EOF marker, and `TYPE`
  stops there.
"""


def _render_hardware_doc(info: dict) -> str:
    """Render the hardware overview from what this bridge actually captured.

    Deliberately not a hardcoded description: the boot banner is the truth for
    whichever board is plugged in, and a fixed doc goes stale or describes a
    different machine.
    """
    unknown = "(not captured yet - reboot the machine to capture its boot banner)"
    out = [
        "# Connected System Overview",
        "",
        "Captured from this machine's RomWBW boot banner by the bridge.",
        "",
        "## Core",
        f"- **RomWBW version**: {info.get('version') or unknown}",
    ]
    if info.get("platform"):
        out.append(f"- **Board**: {info['platform']}"
                   + (f" (build `{info['config']}`)" if info.get("config") else ""))
    out += [
        f"- **CPU**: {info.get('cpu') or unknown}",
        f"- **Memory**: {info.get('memory') or unknown}"
        + (f", {info['mmu']}" if info.get("mmu") else ""),
        f"- **Wait states**: {info.get('wait_states') or unknown}",
        f"- **Interrupt mode**: {info.get('int_mode') or unknown}",
    ]
    if info.get("zsdos_version"):
        out.append(f"- **Operating system**: {info['zsdos_version']}")
    if info.get("cbios_version"):
        out.append(f"- **CBIOS**: {info['cbios_version']}")
    if info.get("tpa"):
        out.append(f"- **TPA (space for programs)**: {info['tpa']}")
    if info.get("timestamp"):
        out.append(f"- **Banner captured**: {info['timestamp']}")

    devices = info.get("devices") or []
    if devices:
        out += ["", "## HBIOS device inventory", ""]
        out += [f"- `{d}`" for d in devices]

    mappings = info.get("drive_mappings") or {}
    if mappings:
        out += ["", "## Drive to device mapping", ""]
        out += [f"- `{letter}:` -> `{device}`" for letter, device in mappings.items()]

    drives = info.get("drives") or []
    if drives:
        out += ["", f"## Catalogued volumes (last scan: {info.get('last_scan_time', 'unknown')})", "",
                "| Drive | Device | Files | Free | Access | Purpose |",
                "|---|---|---|---|---|---|"]
        for d in drives:
            out.append(
                f"| {d.get('drive','')} | {d.get('device','')} | {d.get('files_count','?')} "
                f"| {d.get('free_space','?')} | {d.get('access','?')} | {d.get('purpose','')} |"
            )
        out += ["", "Run `rc2014_scan_drives` to refresh this table."]
    else:
        out += ["", "No volumes catalogued yet - run `rc2014_scan_drives`."]

    survey = info.get("survey") or {}
    if survey:
        out += ["", f"## SURVEY report ({survey.get('timestamp', 'unknown')})", ""]
        if survey.get("tpa_bytes"):
            out.append(f"- **TPA**: {survey['tpa_bytes']} bytes available to programs")
        if survey.get("ram_bytes") is not None:
            out.append(f"- **RAM / ROM in the 64K map**: {survey.get('ram_bytes')} / "
                       f"{survey.get('rom_bytes')} bytes")
        for label, key in (("BIOS", "bios_addr"), ("BDOS", "bdos_addr"), ("iobyte", "iobyte")):
            if survey.get(key):
                out.append(f"- **{label}**: 0x{survey[key]}")
        if survey.get("io_ports"):
            out.append(f"- **Active I/O ports** ({survey.get('io_ports_active', '?')}): "
                       + " ".join(f"0x{p}" for p in survey["io_ports"]))
        if survey.get("drives"):
            out += ["",
                    "Per-drive totals below count **every CP/M user area**, unlike the",
                    "volume table above, which reflects only the user area that was current",
                    "during the scan.", "",
                    "| Drive | Used | Files | Free |", "|---|---|---|---|"]
            for letter, d in survey["drives"].items():
                out.append(f"| {letter}: | {d.get('used','?')} | {d.get('files','?')} "
                           f"| {d.get('free','?')} |")

    out += [
        "",
        "## Serial pacing",
        "",
        "The console runs at 115200 8N1. The bridge paces outbound text one byte",
        "at a time with a ~15ms gap: an unpaced burst overruns the UART's receive",
        "buffer while the Z80 is servicing interrupts, and shows up as dropped",
        "characters. XMODEM blocks are paced in 8-byte chunks for the same reason.",
    ]
    return "\n".join(out)


class McpServer:
    def __init__(self, link, host: str = "0.0.0.0", port: int = 8014,
                 cpnet: Optional[dict] = None):
        self.link = link
        self.host = host
        self.port = port
        # CP/NET push settings from app.py (ini/CLI): share, server_command,
        # port, tools_du, network_map - see rc2014_cpnet_push's docstring.
        self._cpnet = cpnet or {}
        self._server_thread: Optional[threading.Thread] = None
        self._uvicorn: Optional[uvicorn.Server] = None

        self.mcp = MCPServer(
            "RC2014 Bridge",
            instructions=SERVER_INSTRUCTIONS,
        )

        self._register_resources()
        self._register_prompts()
        self._register_tools()

    # ------------------------------------------------------------------
    # resources & prompts
    # ------------------------------------------------------------------
    def _register_resources(self):
        @self.mcp.resource("rc2014://docs/hardware-overview")
        def get_hardware_overview_doc() -> str:
            """This machine's captured hardware configuration: CPU, memory, HBIOS devices, drives, serial pacing."""
            logger.info("MCP Resource requested: rc2014://docs/hardware-overview")
            return _render_hardware_doc(getattr(self.link, "hardware_info", {}) or {})

        @self.mcp.resource("rc2014://docs/cpm-guide")
        def get_cpm_guide_doc() -> str:
            """Quick reference for CP/M 2.2 and ZSDOS commands, utilities, and filename conventions."""
            logger.info("MCP Resource requested: rc2014://docs/cpm-guide")
            return CPM_GUIDE_DOC

        @self.mcp.resource("rc2014://system/hardware-info")
        def get_hardware_info_resource() -> str:
            """Live JSON snapshot of captured hardware specs, OS versions, and drive inventory."""
            logger.info("MCP Resource requested: rc2014://system/hardware-info")
            return json.dumps(getattr(self.link, "hardware_info", {}), indent=2)

    def _register_prompts(self):
        @self.mcp.prompt("rc2014_assistant_instructions")
        def rc2014_assistant_prompt() -> str:
            """Prompt template configuring the model as an RC2014 retro-computing operator."""
            logger.info("MCP Prompt requested: rc2014_assistant_instructions")
            return (
                "You are operating a Z80 retro computer running RomWBW and CP/M through the "
                "rc2014bridge MCP tools.\n\n"
                "Use `rc2014_run_command` for anything that returns to a shell prompt - it sends "
                "the command and gives you back its output in one call. Fall back to "
                "`rc2014_send_text` plus `rc2014_get_screen` only for interactive programs and "
                "the HBIOS boot menu. Use `rc2014_upload_file`/`rc2014_download_file` (host paths) "
                "for file transfer - or `rc2014_upload_files`/`rc2014_download_files` to move "
                "several files in one YMODEM batch - rather than driving XM/ZMD yourself.\n\n"
                "Read `rc2014://docs/hardware-overview` before assuming anything about this "
                "machine's drives or memory. On ZPM3 use `SDZ` not `STAT`/`DIR <user>:`/`ERA`. "
                "Write CP/M filenames as uppercase 8.3. If a tool "
                "returns `busy`, another operation owns the serial wire - wait and retry."
            )

    # ------------------------------------------------------------------
    # progress plumbing
    # ------------------------------------------------------------------
    async def _with_progress(self, ctx: Context, fn, *args, **kwargs):
        """Run a blocking link operation in a worker thread, streaming the
        link's own progress state to the client while it runs."""

        async def _watch():
            try:
                while True:
                    await anyio.sleep(0.5)
                    snap = self.link.progress_snapshot()
                    xmodem, scan = snap["xmodem"], snap["scan"]
                    if xmodem.get("active") and xmodem.get("total_blocks"):
                        await ctx.report_progress(
                            xmodem["current_block"], xmodem["total_blocks"],
                            f"{xmodem.get('direction', 'transfer')} {xmodem.get('filename', '')}: "
                            f"block {xmodem['current_block']}/{xmodem['total_blocks']}",
                        )
                    elif scan.get("active") and scan.get("total"):
                        await ctx.report_progress(
                            scan["index"], scan["total"], f"scanning {scan.get('drive', '')}",
                        )
            except anyio.get_cancelled_exc_class():
                raise
            except Exception:  # noqa: BLE001 - progress is best-effort
                logger.debug("progress reporting stopped", exc_info=True)

        async with anyio.create_task_group() as tg:
            tg.start_soon(_watch)
            try:
                return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
            finally:
                tg.cancel_scope.cancel()

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------
    def _register_tools(self):
        link = self.link

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Run a CP/M command", read_only_hint=False, open_world_hint=True))
        def rc2014_run_command(command: str, timeout: float = 15.0) -> dict:
            """Run one command and return its output. THE tool to use for CP/M work.

            Sends `command`, waits for the prompt to return, and returns only what
            that command printed (the board's echo and the trailing prompt are
            stripped). No polling needed. Examples: 'SDZ A1:', 'SDZ A1:VTUNE.COM',
            'TYPE B:README.TXT'. On ZPM3 do **not** use `STAT` or `ERA` — the bridge
            translates `STAT <target>` to `SDZ <target>` when possible.

            Times out on anything that does not come back to a shell prompt -
            interactive programs like MBASIC or ED, and the HBIOS boot menu. Use
            rc2014_send_text + rc2014_get_screen for those.
            """
            logger.info("MCP Tool called: rc2014_run_command(command=%r, timeout=%s)", command, timeout)
            return link.run_command(command, timeout=timeout)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Read the screen", read_only_hint=True, idempotent_hint=True))
        def rc2014_get_screen(max_lines: int = 40) -> str:
            """Read the terminal screen, newest content last, prefixed with a state line.

            Use for interactive programs, the boot menu, or to see what a human has
            been doing - rc2014_run_command already returns command output. Pass
            max_lines=0 for the entire scrollback (can be thousands of lines).
            """
            logger.info("MCP Tool called: rc2014_get_screen(max_lines=%d)", max_lines)
            sc = link.get_screen(max_lines=max_lines)
            # Screen rows are padded to the full grid width; sending that padding
            # would double the cost of every read for no information.
            lines = [line.rstrip() for line in sc.get("lines", [])]
            while lines and not lines[-1]:
                lines.pop()
            xmodem = sc.get("xmodem_progress", {}) or {}
            transfer = (f"{xmodem.get('direction', 'transfer')} "
                        f"{xmodem.get('current_block', 0)}/{xmodem.get('total_blocks', 0)}"
                        if xmodem.get("active") else "idle")
            header = (f"[state={sc.get('system_state', 'unknown')} "
                      f"os={sc.get('os') or '-'} "
                      f"prompt={sc.get('last_prompt', '')!r} "
                      f"mode={sc.get('mode', 'terminal')} "
                      f"operation={sc.get('current_op') or 'idle'} "
                      f"xmodem={transfer}]")
            return "\n".join([header] + lines)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Send raw text", read_only_hint=False, open_world_hint=True))
        def rc2014_send_text(text: str) -> str:
            """Send text to the console without waiting for a reply. Enter is appended.

            The escape hatch for things rc2014_run_command cannot do: answering a
            program's prompt, driving MBASIC or ED, choosing an entry in the HBIOS
            boot menu. Follow it with rc2014_get_screen or rc2014_wait_for to see
            what happened. For ordinary commands prefer rc2014_run_command; for
            control characters, or to interrupt something, use rc2014_send_keys.
            """
            logger.info("MCP Tool called: rc2014_send_text(text=%r)", text)
            busy = link.busy_reason()
            if busy:
                return f"Not sent - {busy}. Wait for it to finish and retry."
            link.send_text(text, append_enter=True)
            return f"Sent to RC2014: {text!r}"

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Send raw keystrokes", read_only_hint=False, open_world_hint=True))
        def rc2014_send_keys(keys: str) -> dict:
            """Send raw keystrokes with NO Enter appended - including control characters.

            This is how you interrupt something. Unlike every other tool it is
            allowed to run while a transfer or command is in progress, because
            that is the situation it exists for.

            Mnemonics, since control characters can't go in a JSON string:
              ^X ^C ^Z    Ctrl-X, Ctrl-C, Ctrl-Z (^A..^Z, plus ^[ ^] ^?)
              <ESC>       named key: NUL BS TAB LF CR ESC CAN EOF SPACE DEL
              <PAUSE>     brief wait, for sequences that need one
              ^^          a literal caret
            Anything else is sent as typed.

            Recipes:
              '^X<PAUSE>^X'  cancel a stuck XM transfer (XM's own documented hint)
              '^C'           interrupt a running program
              'Y<CR>'        answer a Y/N prompt
              '<ESC>'        leave an editor's insert mode
            """
            logger.info("MCP Tool called: rc2014_send_keys(keys=%r)", keys)
            try:
                return link.send_keys(keys)
            except ValueError as e:
                return {"ok": False, "error": str(e), "keys": keys}

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Wait until ready", read_only_hint=True))
        def rc2014_wait_until_ready(settle: float = 1.0) -> dict:
            """Wait for the machine to finish booting and settle at a prompt.

            Call this after booting a disk (rc2014_send_text with the unit number)
            before sending commands. A boot profile keeps running programs after
            the OS banner appears, and anything sent during that window is simply
            lost - the board is not reading input yet, so a command can arrive
            with its first characters missing.
            """
            logger.info("MCP Tool called: rc2014_wait_until_ready(settle=%s)", settle)
            return link.wait_until_ready(settle=settle)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Wait for output", read_only_hint=True))
        def rc2014_wait_for(pattern: str, timeout: float = 10.0) -> dict:
            """Wait for a regex to appear in output arriving from now on.

            Only searches output produced after this call starts, so an old prompt
            already on screen will not match. Useful after rc2014_send_text - e.g.
            wait for 'Boot \\[H=Help\\]:' after a reboot.
            """
            logger.info("MCP Tool called: rc2014_wait_for(pattern=%r, timeout=%s)", pattern, timeout)
            return link.wait_for(pattern, timeout=timeout)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Upload a host file to the board", read_only_hint=False, destructive_hint=True))
        async def rc2014_upload_file(ctx: Context, host_path: str, drive: str,
                                     user: int = 0, name: str | None = None,
                                     overwrite: bool = False) -> dict:
            """Send a file from the host's filesystem to the RC2014 over XMODEM.

            THE DEFAULT way to upload. The bridge reads host_path itself - never
            inline file content into a tool call. Handles the whole sequence:
            runs XM on the board, transfers raw (no zip staging), confirms with
            DIR. Do NOT run 'XM R' yourself first.

            host_path: full path to the file on the host, e.g. 'Z:\\src\\HELLO.TXT'.
            drive: target drive, 'A'..'P'.
            user: CP/M user area, 0-15 (default 0).
            name: 8.3 CP/M filename on the board; defaults to the host basename
              uppercased. No drive prefix - put the drive in `drive` instead.
            overwrite: XM refuses to replace an existing file, so an existing
              target is erased first. Pass false to fail instead.
            Bytes are sent verbatim (no CRLF or EOF transcoding); the final
              128-byte block is 0x1A-padded, which covers CP/M text EOF.
            Requires the machine to be at a CP/M prompt.
            """
            logger.info("MCP Tool called: rc2014_upload_file(host_path=%r, drive=%r, "
                        "user=%d, name=%r, overwrite=%s)", host_path, drive, user,
                        name, overwrite)
            return await self._with_progress(
                ctx, link.upload_file, host_path, drive, user=user, name=name,
                overwrite=overwrite)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Download a board file to the host", read_only_hint=False, destructive_hint=True))
        async def rc2014_download_file(ctx: Context, name: str, drive: str,
                                       user: int = 0, dest_path: str | None = None) -> dict:
            """Copy a file off the RC2014 over XMODEM (1K blocks) to a host path.

            THE DEFAULT way to download. The bridge writes dest_path itself and
            returns only metadata (path, size, sha256) - no file content crosses
            MCP. Do NOT run 'XM S' yourself first.

            name: 8.3 CP/M filename ONLY on the board, e.g. 'LEDSHOW.COM' - no
              drive prefix. Put the drive in `drive` instead.
            drive: source drive, 'A'..'P'.
            user: CP/M user area, 0-15 (default 0).
            dest_path: where to write on the host; defaults to the CP/M filename
              in the bridge's working directory. Overwritten without asking.
            Bytes are written verbatim, including any 0x1A padding CP/M's
              128-byte records carry past the logical end of file.
            Requires the machine to be at a CP/M prompt.
            """
            logger.info("MCP Tool called: rc2014_download_file(name=%r, drive=%r, "
                        "user=%d, dest_path=%r)", name, drive, user, dest_path)
            return await self._with_progress(
                ctx, link.download_file, name, drive, user=user, dest_path=dest_path)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Upload host files to the board (batch YMODEM)", read_only_hint=False,
            destructive_hint=True))
        async def rc2014_upload_files(ctx: Context, host_paths: list[str], drive: str,
                                      user: int = 0, overwrite: bool = False) -> dict:
            """Send several files from the host's filesystem to the RC2014 in one
            YMODEM batch (ZMD on the board).

            Prefer this over rc2014_upload_file when moving more than one file: a
            single arming covers the whole batch, blocks are 1K in both
            directions, and each file's length travels in its header. The bridge
            reads the host paths itself - never inline file content into a tool
            call. Handles the whole sequence: switches the board to the target
            drive/user, runs 'ZMD R', transfers, verifies with a listing,
            restores the previous drive/user. Do NOT run 'ZMD R' yourself first.

            NOTE: when ZMD exits it cold-starts this board (RomWBW banner +
            autoboot back to the OS, ~11s). That is expected - not a crash -
            and the tool waits it out before verifying. Do not send anything
            to the console while a ZMD transfer is settling.

            host_paths: full paths on the host, e.g. ['Z:\\src\\HELLO.TXT',
              'Z:\\src\\GAME.COM']. The CP/M name of each file is its host
              basename uppercased and must be valid 8.3.
            drive: target drive, 'A'..'P'.
            user: CP/M user area, 0-15 (default 0).
            overwrite: an existing target fails the whole batch (before anything
              is armed); pass true to erase existing targets first.
            Bytes are sent verbatim (no CRLF or EOF transcoding).
            Requires the machine to be at a CP/M prompt.
            """
            logger.info("MCP Tool called: rc2014_upload_files(host_paths=%r, drive=%r, "
                        "user=%d, overwrite=%s)", host_paths, drive, user, overwrite)
            return await self._with_progress(
                ctx, link.upload_files, host_paths, drive, user=user, overwrite=overwrite)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Download board files to the host (batch YMODEM)", read_only_hint=False,
            destructive_hint=True))
        async def rc2014_download_files(ctx: Context, names: list[str], drive: str,
                                        user: int = 0, dest_dir: str = ".",
                                        strip_padding: bool = True) -> dict:
            """Copy several files off the RC2014 in one YMODEM batch to a host
            directory (ZMD on the board).

            Prefer this over rc2014_download_file when fetching more than one
            file: a single arming covers the whole batch. The bridge writes
            dest_dir itself and returns only metadata (paths, sizes, sha256) -
            no file content crosses MCP. Do NOT run 'ZMD SB' yourself first.
            The bridge arms ZMD with the SB verb (B forces batch mode, so a
            single-file download still sends a block-0 header - plain 'ZMD S'
            with one file silently falls back to XMODEM-1K and sends none).

            NOTE on sizes: ZMD advertises 1K-rounded sizes in its headers
            (CP/M only knows 128-byte record counts), so the header alone
            cannot trim a ZMD send byte-exactly. strip_padding=true (the
            default, same convention as rc2014_download_file) also strips
            trailing 0x1A - right for text. Pass false for byte-exact binary,
            where CP/M's 128-byte record granularity still applies (a file
            whose true length isn't a multiple of 128 keeps its padding).

            NOTE: when ZMD exits it cold-starts this board (RomWBW banner +
            autoboot back to the OS, ~11s). That is expected - not a crash -
            and the tool waits it out before returning. Do not send anything
            to the console while a ZMD transfer is settling. ZMD 1.50 S stays
            armed after its last file instead of exiting, so the receiver
            sends one final 'C' to end the batch; ZMD's CAN CAN + "-- File
            transfer aborted" answer is expected and reported as success once
            every requested file has arrived intact.

            names: 8.3 CP/M filenames ONLY on the board, e.g. ['LEDSHOW.COM',
              'README.TXT'] - no drive prefixes. Put the drive in `drive`
              instead. All names must exist or the batch fails before arming.
            drive: source drive, 'A'..'P'.
            user: CP/M user area, 0-15 (default 0).
            dest_dir: host directory to write into (created if missing);
              defaults to the bridge's working directory. Existing files there
              are overwritten without asking.
            The board is switched to the source drive/user first and restored
            afterwards. Note CP/M's ~127-character command-line limit bounds
            how many names fit in one batch.
            Requires the machine to be at a CP/M prompt.
            """
            logger.info("MCP Tool called: rc2014_download_files(names=%r, drive=%r, "
                        "user=%d, dest_dir=%r, strip_padding=%s)", names, drive, user,
                        dest_dir, strip_padding)
            return await self._with_progress(
                ctx, link.download_files, names, drive, user=user, dest_dir=dest_dir,
                strip_padding=strip_padding)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Upload a file to the board", read_only_hint=False, destructive_hint=True))
        async def rc2014_upload(ctx: Context, name: str, drive: str, user: int = 0,
                                content: str = "", binary: bool = False,
                                overwrite: bool = False) -> dict:
            """Copy INLINE content to the RC2014, zipped over XMODEM.

            Prefer rc2014_upload_file for anything already on disk - this tool
            exists for content that only lives in the conversation, and it pays
            for that: the whole file travels inside the MCP call (base64 +33%
            when binary=true). Handles the whole sequence: zips content, runs XM
            on the board, transfers, UNZIPs, confirms with DIR. Do NOT run
            'XM R' yourself first.

            name: 8.3 CP/M filename ONLY, e.g. 'HELLO.TXT' - no drive prefix
              (unlike cpm_path-style tools such as rc2014_read_text_file).
              Put the drive in `drive` instead.
            drive: target drive, 'A'..'P'.
            user: CP/M user area, 0-15 (default 0).
            content: text, or base64 when binary=true. binary=false encodes as
              latin-1 - characters outside it (em-dashes, curly quotes, most
              non-ASCII text) are silently replaced with '?'. Use binary=true
              (base64) for anything that must survive exactly.
            binary: false (default) applies CP/M's CRLF + 0x1A EOF-marker
              convention for text; true passes raw bytes through untouched.
            overwrite: UNZIP (like XM) refuses to replace an existing file, so
              an existing target is erased first. Pass false to fail instead.
            Requires the machine to be at a CP/M prompt.
            """
            logger.info("MCP Tool called: rc2014_upload(name=%r, drive=%r, user=%d, "
                        "binary=%s, overwrite=%s)", name, drive, user, binary, overwrite)
            return await self._with_progress(
                ctx, link.upload, name, drive, user=user, content=content,
                binary=binary, overwrite=overwrite)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Download a file from the board", read_only_hint=False, destructive_hint=True))
        async def rc2014_download(ctx: Context, name: str, drive: str, user: int = 0,
                                  binary: bool = False) -> dict:
            """Copy a file off the RC2014 over XMODEM (1K blocks), content inline.

            Prefer rc2014_download_file - it writes to a host path directly and
            no content crosses MCP. This tool returns the whole file inline in
            the response (base64 when binary=true), which burns context fast on
            anything but tiny files.

            Handles the whole sequence: runs XM on the board, receives, returns the
            content and its sha256. Do NOT run 'XM S' yourself first.

            name: 8.3 CP/M filename ONLY on the board, e.g. 'LEDSHOW.COM' - no
              drive prefix (unlike cpm_path-style tools such as
              rc2014_read_text_file). Put the drive in `drive` instead.
            drive: source drive, 'A'..'P'.
            user: CP/M user area, 0-15 (default 0).
            binary: false (default) returns text (latin-1 decoded - anything
              outside it comes back mangled, matching how it was written);
              true returns base64, byte-exact only to the file's 128-byte
              CP/M record size (CP/M has no sub-record length anywhere, so a
              file whose true length isn't a multiple of 128 may come back
              with trailing padding).
            Requires the machine to be at a CP/M prompt.
            """
            logger.info("MCP Tool called: rc2014_download(name=%r, drive=%r, user=%d, "
                        "binary=%s)", name, drive, user, binary)
            return await self._with_progress(
                ctx, link.download, name, drive, user=user, binary=binary)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Read a text file", read_only_hint=True))
        def rc2014_read_text_file(cpm_path: str, max_bytes: int = 8192) -> dict:
            """Read a text file off the board with TYPE - no transfer needed.

            Cheap, but not byte-exact: TYPE expands tabs and stops at the 0x1A EOF
            marker. Use rc2014_download when the exact bytes matter (any binary,
            or a file you intend to write back).
            """
            logger.info("MCP Tool called: rc2014_read_text_file(cpm_path=%r)", cpm_path)
            return link.read_text_file(cpm_path, max_bytes=max_bytes)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Push a file to the board via CP/NET", read_only_hint=False,
            open_world_hint=True))
        def rc2014_cpnet_push(host_path: str, target_du: str = "A0:",
                              name: str = "", overwrite: bool = True) -> dict:
            """Copy a file from this PC to a CP/M drive on the board over CP/NET
            (W5500 Ethernet) - ~5x faster than XMODEM (~4.7 KB/s vs ~0.9 KB/s)
            and no transfer protocol hogging the console.

            Fall back to rc2014_upload_file (XMODEM) when CP/NET is unavailable
            (no W5500, or network bring-up hasn't run on the board).

            How it works: the file is staged into the CP/NET share (a host
            directory served as drive C: by a CP/NET socket server such as
            CpnetSocketServer; auto-started when cpnet-server-command is set),
            then on the board NDOS3 + 'NETWORK <map>' are ensured and
            'COPY [<target_du>=]<remote>0:<name>' pulls the file in (the
            destination defaults to the current drive/user, and the board is
            only switched off its DU when the target differs). Remote files
            live in USER 0: always <drive>0:, never a bare drive letter.
            Listing a network drive with SDZ/DIR is fine while the link is
            healthy, but a *failing* dir-list may retry forever and wedge
            the console (ZPM3 VERROR auto-retries on error) - which is why
            this tool probes residency with RSXLS instead.

            Prerequisites: a W5500 socket on the board configured for the
            server (WIZCFG - note WIZRESET wipes it), and these bridge settings
            (rc2014bridge.ini keys or same-named CLI flags):
              cpnet-share           host staging directory (required)
              cpnet-server-command  server launcher, auto-started if down
              cpnet-port            server TCP port (default 31100)
              cpnet-tools-du        drive/user holding NDOS3/NETWORK, e.g.
                                    'A4:' (default: rely on the system path)
              cpnet-network-map     NETWORK mapping (default 'K:=C:[0]'; the
                                    letter before := becomes the remote drive)

            host_path: file on this PC. target_du: CP/M drive/user like 'A4:'.
            name: 8.3 CP/M name; defaults to the host basename
            uppercased/truncated. overwrite (default True): erase an existing
            target file first so COPY can't stall on its "Replace (Y/[N])?"
            prompt.
            """
            import os as _os, re as _re, shutil, socket, subprocess, time as _time
            logger.info("MCP Tool called: rc2014_cpnet_push(host_path=%r, "
                        "target_du=%r, name=%r)", host_path, target_du, name)
            if not _os.path.isfile(host_path):
                return {"ok": False, "error": f"no such file: {host_path}"}
            cfg = self._cpnet
            share = cfg.get("share") or ""
            if not share:
                return {"ok": False, "error": "CP/NET share not configured - set "
                        "cpnet-share in rc2014bridge.ini (or --cpnet-share) to "
                        "the host directory your CP/NET server serves as C:"}
            port = int(cfg.get("port") or 31100)
            server_cmd = cfg.get("server_command") or ""
            net_map = (cfg.get("network_map") or "K:=C:[0]").strip()
            mm = _re.match(r"([A-Pa-p])\s*:=", net_map)
            if not mm:
                return {"ok": False, "error": "cannot parse cpnet-network-map "
                        f"{net_map!r} (expected something like 'K:=C:[0]')"}
            remote_du = mm.group(1).upper() + "0:"

            base = _os.path.basename(host_path).upper()
            stem, dot, ext = base.partition(".")
            board_name = name or (stem[:8] + (dot + ext[:3] if dot else ""))

            # 1. socket server up?
            def _server_up():
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=2).close()
                    return True
                except OSError:
                    return False
            if not _server_up():
                if not server_cmd:
                    return {"ok": False, "error": f"CP/NET server not listening "
                            f"on :{port} and no cpnet-server-command configured "
                            "to launch it"}
                if not _os.path.isfile(server_cmd):
                    return {"ok": False, "error": "CP/NET server down and "
                            f"launcher not found: {server_cmd}"}
                subprocess.Popen(["cmd.exe", "/c", "start", "", server_cmd])
                for _ in range(10):
                    _time.sleep(1)
                    if _server_up():
                        break
                else:
                    return {"ok": False, "error": "CP/NET socket server "
                            f"did not start listening on :{port}"}

            # 2. stage the file into the share
            _os.makedirs(share, exist_ok=True)
            shutil.copyfile(host_path, _os.path.join(share, board_name))

            # 3. NDOS3 resident? Probe with RSXLS, never SDZ/DIR on a network
            # drive - a *failing* dir-list can retry forever (see docstring).
            r = link.run_command("RSXLS", timeout=10)
            if "NDOS3" not in r.get("output", ""):
                tools_du = (cfg.get("tools_du") or "").strip()
                if tools_du:
                    link.run_command(tools_du, timeout=5)
                r2 = link.run_command("NDOS3", timeout=15)
                out2 = r2.get("output", "")
                # "already loaded" means the RSXLS probe false-negatived
                # (console still settling) - NDOS3 is resident, which is fine.
                if "NDOS3 Started" not in out2 and "already loaded" not in out2:
                    return {"ok": False, "error": "NDOS3 failed to load",
                            "detail": out2}
                r3 = link.run_command(f"NETWORK {net_map}", timeout=15)
                if not r3.get("ok"):
                    return {"ok": False, "error": f"NETWORK {net_map} failed",
                            "detail": r3.get("output", "")}

            # 4. pre-erase on the target (ZPM3 ERASE wants a bare name on the
            # current DU) so COPY never stalls on a "Replace (Y/[N])?" prompt.
            # r["prompt"] is the raw ANSI prompt (ESC[1m17:44ESC[m A4ESC[1mESC[m>)
            # - parse a clean DU out of it; sending the raw string back types
            # escape garbage on the console (the "[1m17:44[m A4[1m[m>" noise).
            # No chatter: switch DU only when the board isn't already on the
            # target, restore only when we actually switched.
            _clean = _re.sub(r"\x1b\[[0-9;]*m", "", r.get("prompt") or "")
            _m = _re.search(r"([A-P])(\d{0,2})(?::[A-Za-z0-9]+)?\s*>", _clean)
            prev_du = (_m.group(1) + _m.group(2) + ":") if _m else None
            tm = _re.match(r"([A-Pa-p])(\d{0,2})\s*:", target_du.strip())
            if not tm:
                return {"ok": False, "error": f"bad target_du {target_du!r} "
                        "(want a CP/M drive/user like 'A4:')"}
            target_norm = tm.group(1).upper() + tm.group(2) + ":"
            on_target = prev_du == target_norm
            switched = False
            if overwrite:
                if not on_target:
                    link.run_command(target_norm, timeout=5)
                    switched = True
                link.run_command(f"ERASE {board_name}", timeout=10)
                on_target = True

            # 5. pull the file: ~4.7 KB/s, size the timeout accordingly. COPY
            # defaults the destination to the current DU, so once the board is
            # known to sit on the target the bare source form is enough.
            size = _os.path.getsize(host_path)
            copy_timeout = max(60.0, size / 3500.0 + 30.0)
            src = f"{remote_du}{board_name}"
            copy_cmd = f"COPY {src}" if on_target else f"COPY {target_norm}={src}"
            r4 = link.run_command(copy_cmd, timeout=copy_timeout)
            out = r4.get("output", "")
            if switched and prev_du:
                link.run_command(prev_du, timeout=5)
            if "0 Errors" not in out:
                return {"ok": False, "error": "COPY failed or timed out (is "
                        "the W5500 socket for the CP/NET server configured? "
                        "WIZRESET wipes it - re-run WIZCFG)",
                        "detail": out, "timed_out": r4.get("timed_out")}
            result = {"ok": True, "name": board_name, "target": target_norm,
                      "bytes": size, "duration_s": r4.get("duration_s"),
                      "detail": out}
            if switched and prev_du is None:
                result["note"] = ("could not parse the pre-copy prompt, so the "
                                  f"board was left on {target_norm} instead of "
                                  "restoring the previous drive")
            return result

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Catalogue the drives", read_only_hint=True))
        async def rc2014_scan_drives(ctx: Context) -> dict:
            """Catalogue every mapped drive: free space, access mode, file sample, purpose.

            Runs a directory listing per drive (SDZ on ZPM3, DIR+STAT on classic
            CP/M), so it takes tens of seconds and reports progress as it goes.
            Results are cached in hardware_info - for a single drive,
            'SDZ A1:' via rc2014_run_command is much faster.

            NOTE: file names and counts here cover the **current CP/M user area
            only** (usually 0). A drive can hold far more - this system's A: shows
            50 files in user 0 but 210 in total. Use rc2014_survey for true
            per-drive totals.
            Requires the machine to be at a CP/M prompt.
            """
            logger.info("MCP Tool called: rc2014_scan_drives()")
            return await self._with_progress(ctx, link.scan_drives)

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Run SURVEY", read_only_hint=True))
        def rc2014_survey() -> dict:
            """Run RomWBW's SURVEY and return the system report, structured.

            One command (~7s) and the best single source of system detail:
            per-drive bytes used / file count / bytes free counting **all CP/M
            user areas**, the 64K memory map with BIOS and BDOS addresses, TPA
            size, and the active I/O port map.

            Complements rc2014_scan_drives, which is slower but returns actual
            filenames - and only for the current user area.
            Requires the machine to be at a CP/M prompt.
            """
            logger.info("MCP Tool called: rc2014_survey()")
            return link.survey()

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Get hardware info", read_only_hint=True, idempotent_hint=True))
        def rc2014_get_hardware_info() -> dict:
            """Captured hardware: RomWBW version, CPU, memory/MMU, HBIOS devices, OS
            versions, drive mappings, and the last drive catalogue."""
            logger.info("MCP Tool called: rc2014_get_hardware_info()")
            return getattr(link, "hardware_info", {})

        @self.mcp.tool(annotations=ToolAnnotations(
            title="Reboot the machine", read_only_hint=False, destructive_hint=True))
        def rc2014_reboot() -> dict:
            """Reboot the RC2014, choosing the right method for its current state
            (C:REBOOT /C at a CP/M prompt, R at the HBIOS boot menu).

            Anything unsaved on the RAM disk survives a reboot but not a power cycle.
            Follow with rc2014_wait_for on the boot banner to know when it is back.
            """
            logger.info("MCP Tool called: rc2014_reboot()")
            return link.reboot()

    # ------------------------------------------------------------------
    # transports
    # ------------------------------------------------------------------
    def _build_app(self):
        # DNS rebinding protection off and CORS wide open: this is a LAN tool on
        # a trusted network by design. See the security note in README.md.
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
            allowed_hosts=["*"],
            allowed_origins=["*"],
        )

        # stateless_http=True: every request gets its own fresh transport and
        # connection, for every protocol version - no Mcp-Session-Id is ever
        # minted or expected. 2026-07-28+ clients already route to the
        # single-exchange handler unconditionally on protocol version; this
        # flag is what removes the legacy stateful path for everything else.
        app = self.mcp.streamable_http_app(
            host=self.host, stateless_http=True, transport_security=security)

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        return app

    def endpoints(self) -> list[str]:
        return [f"http://{self.host}:{self.port}/mcp"]

    def start(self):
        def _run_server():
            try:
                logger.info("Starting MCP server on %s:%d", self.host, self.port)
                config = uvicorn.Config(self._build_app(), host=self.host,
                                        port=self.port, log_level="info")
                self._uvicorn = uvicorn.Server(config)
                self._uvicorn.run()
            except Exception as e:
                logger.exception("MCP Server error: %s", e)

        self._server_thread = threading.Thread(target=_run_server, daemon=True)
        self._server_thread.start()

    def stop(self):
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        if self._server_thread is not None:
            self._server_thread.join(timeout=3.0)
            if self._server_thread.is_alive():
                logger.warning("MCP server thread did not stop within 3s")
