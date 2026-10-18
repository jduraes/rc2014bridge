import argparse
import logging
import sys

from rc2014bridge import config as bridge_config
from rc2014bridge.display import run as run_display
from rc2014bridge.link import SerialLink
from rc2014bridge.mcp_server import McpServer

DEFAULT_LOG = "rc2014bridge.log"
DEFAULT_HW_INFO = "hardware_info.json"


class _SafeStreamHandler(logging.StreamHandler):
    """StreamHandler that survives the console handle going away.

    When the bridge's process ends up detached from a usable console,
    stderr flushes fail with OSError EINVAL and the stock handler answers
    every log record with a full '--- Logging error ---' traceback of its
    own - far noisier than whatever was being logged. Swallow it instead;
    the FileHandler still has the record.
    """

    def emit(self, record):
        try:
            super().emit(record)
        except (OSError, ValueError):
            pass

    def handleError(self, record):
        pass


def _cfg_str(cfg: dict, key: str, fallback):
    val = cfg.get(key)
    return val if val else fallback


def _cfg_int(cfg: dict, key: str, fallback: int) -> int:
    val = cfg.get(key)
    if not val:
        return fallback
    try:
        return int(val)
    except ValueError:
        print(f"Config: {key}={val!r} is not an integer, using default {fallback}", file=sys.stderr)
        return fallback


def _cfg_bool(cfg: dict, key: str, fallback: bool) -> bool:
    val = cfg.get(key)
    if not val:
        return fallback
    return val.strip().lower() in ("1", "true", "yes", "on")


def main():
    # First pass just locates --config, so its contents can seed every other
    # flag's default before the real parser (with --help, choices, etc.) is
    # built. A flag given on the actual command line always wins over
    # whatever the config file says - see the p.add_argument() calls below,
    # which only use the config file to fill in `default=`.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=bridge_config.DEFAULT_CONFIG_PATH)
    pre_args, _ = pre.parse_known_args()
    cfg = bridge_config.load(pre_args.config)

    p = argparse.ArgumentParser(description="RC2014 serial bridge - GUI terminal + MCP server")
    p.add_argument("--config", default=pre_args.config,
                    help="Path to an INI config file providing defaults for the flags below "
                         "(default: %(default)s; a missing file just means the built-in "
                         "defaults apply). Any flag given on the command line overrides it.")
    p.add_argument("--port", default=_cfg_str(cfg, "port", "/dev/ttyUSB0"))
    p.add_argument("--baud", type=int, default=_cfg_int(cfg, "baud", 115200))
    # 160 columns is twice the console's own 80, which makes for a comfortably
    # wide window without shrinking the terminal or growing its height. The board
    # still emits 80-column output; the extra width just isn't used by it.
    p.add_argument("--cols", type=int, default=_cfg_int(cfg, "cols", 160))
    p.add_argument("--rows", type=int, default=_cfg_int(cfg, "rows", 48))
    p.add_argument("--log-file", default=_cfg_str(cfg, "log-file", DEFAULT_LOG), help="Path to log file")
    p.add_argument("--hw-info", default=_cfg_str(cfg, "hw-info", DEFAULT_HW_INFO),
                   help="Path to the captured hardware info JSON (default: %(default)s)")
    p.add_argument("--mcp", action="store_true", default=_cfg_bool(cfg, "mcp", True),
                   help="Enable the MCP server (default: True)")
    p.add_argument("--no-mcp", dest="mcp", action="store_false", help="Disable the MCP server")
    p.add_argument("--mcp-host", default=_cfg_str(cfg, "mcp-host", "0.0.0.0"),
                   help="Host IP to bind the MCP server (default: %(default)s)")
    p.add_argument("--mcp-port", type=int, default=_cfg_int(cfg, "mcp-port", 8014),
                   help="Port for the MCP server (default: %(default)s)")
    p.add_argument("--xmodem-pacing", metavar="CHUNK:DELAY_MS", default=_cfg_str(cfg, "xmodem-pacing", None),
                   help="Override XMODEM write pacing, e.g. 128:2. Default: whatever a "
                        "previous pacing calibration proved on this machine, else 8:10")
    p.add_argument("--text-pacing", metavar="CHUNK:DELAY_MS", default=_cfg_str(cfg, "text-pacing", None),
                   help="Override keystroke/command write pacing, e.g. 8:5 (default 1:15)")
    p.add_argument("--rtscts", action="store_true", default=_cfg_bool(cfg, "rtscts", False),
                   help="Enable hardware RTS/CTS flow control on the serial port. "
                        "Experimental: only meaningful if both the board and the cable "
                        "actually wire CTS/RTS through (default: off)")
    p.add_argument("--xm-command", metavar="CMD", default=_cfg_str(cfg, "xm-command", ""),
                   help="Override how XM is invoked for transfers, e.g. 'XM' to use the "
                        "current drive's copy instead of the ROM disk's (default: ROM disk)")
    p.add_argument("--zmd-command", metavar="CMD", default=_cfg_str(cfg, "zmd-command", ""),
                   help="Override how ZMD is invoked for YMODEM batch transfers, e.g. 'ZMD' "
                        "to use the current drive's copy instead of the ROM disk's "
                        "(default: ROM disk)")
    p.add_argument("--verbose", "-v", action="store_true", default=_cfg_bool(cfg, "verbose", False),
                   help="Enable DEBUG level logging")
    p.add_argument("--pixel-stream", action="store_true", default=_cfg_bool(cfg, "pixel-stream", True),
                   help="Auto-detect Mandel Pixel-Stream banners (0xFA) and switch the "
                        "display/reader to pixel_stream mode (default: on)")
    p.add_argument("--no-pixel-stream", dest="pixel_stream", action="store_false",
                   help="Never auto-switch on 0xFA - use when no Mandel renderer is in play, "
                        "so a stray 0xFA (serial noise, binary output) can't eat the console")
    p.add_argument("--cpnet-share", metavar="DIR", default=_cfg_str(cfg, "cpnet-share", ""),
                   help="Host directory staged into the CP/NET share for rc2014_cpnet_push "
                        "(default: unset - the tool refuses to run until one is set)")
    p.add_argument("--cpnet-server-command", metavar="PATH", default=_cfg_str(cfg, "cpnet-server-command", ""),
                   help="Launcher used to auto-start the CP/NET socket server when it is not "
                        "listening (default: unset - no auto-start)")
    p.add_argument("--cpnet-port", type=int, default=_cfg_int(cfg, "cpnet-port", 31100),
                   help="TCP port of the CP/NET socket server (default: %(default)s)")
    p.add_argument("--cpnet-tools-du", metavar="DU:", default=_cfg_str(cfg, "cpnet-tools-du", ""),
                   help="Drive/user holding NDOS3/NETWORK, e.g. 'A4:' - switch there before "
                        "loading NDOS3 (default: rely on the system search path)")
    p.add_argument("--cpnet-network-map", metavar="MAP", default=_cfg_str(cfg, "cpnet-network-map", "K:=C:[0]"),
                   help="NETWORK mapping for the CP/NET share (default: %(default)s; the "
                        "letter before := becomes the remote drive, user 0)")
    args = p.parse_args()

    def _pacing(value):
        if not value:
            return None
        chunk, _, delay_ms = value.partition(":")
        return int(chunk), float(delay_ms) / 1000.0

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(args.log_file, mode="a"),
            _SafeStreamHandler(sys.stderr),
        ],
    )
    # The MCP library logs every stateless session teardown at INFO
    # ("Terminating session: None") - pure noise on a single-client bridge.
    logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)
    logging.info("Starting RC2014 Bridge on %s @ %d baud", args.port, args.baud)

    link = SerialLink(args.port, baud=args.baud, cols=args.cols, rows=args.rows,
                      hw_info_file=args.hw_info,
                      text_pacing=_pacing(args.text_pacing),
                      xmodem_pacing=_pacing(args.xmodem_pacing),
                      rtscts=args.rtscts,
                      xm_command=args.xm_command,
                      zmd_command=args.zmd_command,
                      pixel_stream=args.pixel_stream)

    mcp_server = None
    if args.mcp:
        try:
            cpnet = {"share": args.cpnet_share,
                     "server_command": args.cpnet_server_command,
                     "port": args.cpnet_port,
                     "tools_du": args.cpnet_tools_du,
                     "network_map": args.cpnet_network_map}
            mcp_server = McpServer(link, host=args.mcp_host, port=args.mcp_port,
                                   cpnet=cpnet)
            mcp_server.start()
            for url in mcp_server.endpoints():
                logging.info("MCP endpoint: %s", url)
        except Exception as e:
            logging.warning("Failed to start MCP server: %s", e)

    try:
        # An explicit --cols/--rows pins the grid, so the GUI must neither
        # restore nor persist a window size. argparse defaults are
        # config-seeded, so after parsing an explicit flag is
        # indistinguishable from a config default - scan argv instead.
        grid_from_cli = any(a.split("=", 1)[0] in ("--cols", "--rows") for a in sys.argv[1:])
        run_display(link, config_path=args.config, window_size_from_cli=grid_from_cli)
    finally:
        if mcp_server:
            mcp_server.stop()
        link.close()
        logging.info("RC2014 Bridge shutdown complete")


if __name__ == "__main__":
    main()
