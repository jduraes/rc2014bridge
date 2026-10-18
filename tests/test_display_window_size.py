"""Tests for _saved_window_size (window-size persistence, display.py)."""

from rc2014bridge.display import (
    SCROLLBAR_W,
    STATUS_BAR_HEIGHT,
    TOP_MENU_HEIGHT,
    _saved_window_size,
)

CELL_W, CELL_H = 10, 20
MIN_W = 20 * CELL_W + SCROLLBAR_W          # minimum grid + scrollbar
MIN_H = TOP_MENU_HEIGHT + 5 * CELL_H + STATUS_BAR_HEIGHT


def _write(tmp_path, text):
    p = tmp_path / "rc2014bridge.ini"
    p.write_text(text)
    return str(p)


def test_missing_file_returns_none(tmp_path):
    assert _saved_window_size(str(tmp_path / "nope.ini"), CELL_W, CELL_H) is None


def test_no_window_section_returns_none(tmp_path):
    p = _write(tmp_path, "[serial]\nport = COM10\n")
    assert _saved_window_size(p, CELL_W, CELL_H) is None


def test_valid_size_is_returned(tmp_path):
    p = _write(tmp_path, "[window]\nwidth = 1200\nheight = 700\n")
    assert _saved_window_size(p, CELL_W, CELL_H) == (1200, 700)


def test_other_sections_do_not_provide_size(tmp_path):
    # Keys are flattened across sections; width/height under [window] are
    # the size - a stray same-named key elsewhere must not count alone.
    p = _write(tmp_path, "[serial]\nwidth = 1200\nheight = 700\n")
    assert _saved_window_size(p, CELL_W, CELL_H) == (1200, 700)


def test_non_integer_rejected(tmp_path):
    p = _write(tmp_path, "[window]\nwidth = abc\nheight = 700\n")
    assert _saved_window_size(p, CELL_W, CELL_H) is None


def test_zero_and_negative_rejected(tmp_path):
    p = _write(tmp_path, "[window]\nwidth = 0\nheight = -5\n")
    assert _saved_window_size(p, CELL_W, CELL_H) is None


def test_too_small_for_minimum_grid_rejected(tmp_path):
    p = _write(tmp_path, f"[window]\nwidth = {MIN_W - 1}\nheight = {MIN_H - 1}\n")
    assert _saved_window_size(p, CELL_W, CELL_H) is None


def test_exactly_minimum_size_accepted(tmp_path):
    p = _write(tmp_path, f"[window]\nwidth = {MIN_W}\nheight = {MIN_H}\n")
    assert _saved_window_size(p, CELL_W, CELL_H) == (MIN_W, MIN_H)


def test_partial_window_section_rejected(tmp_path):
    p = _write(tmp_path, "[window]\nwidth = 1200\n")
    assert _saved_window_size(p, CELL_W, CELL_H) is None
