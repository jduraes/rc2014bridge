import unittest
import threading
from rc2014bridge.display import MENU_DATA


class TestGuiXmodem(unittest.TestCase):
    def test_menu_data_structure(self):
        titles = [m["title"] for m in MENU_DATA]
        self.assertIn("File", titles)
        self.assertIn("Transfer", titles)
        self.assertIn("View", titles)

        file_actions = [item["action"] for item in MENU_DATA[0]["items"]]
        self.assertIn("QUIT", file_actions)

        transfer_actions = [item["action"] for item in MENU_DATA[1]["items"]]
        self.assertIn("PROMPT_SEND", transfer_actions)
        self.assertIn("PROMPT_RECEIVE", transfer_actions)
        # YMODEM batch items next to them, XMODEM ones untouched.
        self.assertIn("PROMPT_SEND_BATCH", transfer_actions)
        self.assertIn("PROMPT_RECEIVE_BATCH", transfer_actions)
        labels = [item["label"] for item in MENU_DATA[1]["items"]]
        self.assertTrue(any("YMODEM" in l and "F10" in l for l in labels), labels)
        self.assertTrue(any("YMODEM" in l and "F11" in l for l in labels), labels)

    def test_async_xmodem_helpers_exist(self):
        from rc2014bridge.link import SerialLink
        self.assertTrue(hasattr(SerialLink, "xmodem_send_async"))
        self.assertTrue(hasattr(SerialLink, "xmodem_receive_async"))

    def test_batch_helpers_exist(self):
        """The batch menu items drive the full composites (arm ZMD, transfer,
        wait out ZMD's exit cold start, verify), not raw protocol calls."""
        from rc2014bridge.link import SerialLink
        from rc2014bridge.display import _open_system_files_dialog, _gui_target_du
        self.assertTrue(hasattr(SerialLink, "upload_files"))
        self.assertTrue(hasattr(SerialLink, "download_files"))
        self.assertTrue(callable(_open_system_files_dialog))
        self.assertTrue(callable(_gui_target_du))


if __name__ == "__main__":
    unittest.main()
