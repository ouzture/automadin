from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from automadin.engine.windows_input import (
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MOVE,
    WM_CANCELMODE,
    WM_CHAR,
    WM_ACTIVATE,
    WM_ACTIVATEAPP,
    WM_KILLFOCUS,
    WM_LBUTTONDBLCLK,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_MOUSEMOVE,
    WM_MOUSEWHEEL,
    WM_NCACTIVATE,
    WM_SETFOCUS,
    WindowsInputPrivilegeError,
    _activate_foreground_window,
    _pack_point,
    NativeWindowsInputBackend,
    click_window,
    drag_window,
    ensure_windows_input_access,
    focus_window,
    press_window_keys,
    scroll_window,
    type_window_text,
)


class FakeBackend:
    def __init__(self) -> None:
        self.messages: list[tuple[int, int, int, int]] = []
        self.focused: list[int] = []
        self.cursor_positions: list[tuple[int, int]] = []
        self.mouse_events: list[int] = []
        self.input_events: list[int] = []
        self.key_events: list[tuple[int, bool]] = []
        self.unicode_events: list[tuple[int, bool]] = []
        self.wheel_events: list[int] = []
        self.cursor = (500, 600)
        self.foreground = 99

    def window_origin(self, hwnd: int) -> tuple[int, int]:
        return 100, 200

    def resolve_message_target(
        self, hwnd: int, screen_x: int, screen_y: int
    ) -> tuple[int, int, int]:
        if (hwnd, screen_x, screen_y) == (10, 125, 240):
            return 20, 7, 9
        return 20, screen_x - 100, screen_y - 200

    def post_message(self, hwnd: int, message: int, wparam: int, lparam: int) -> bool:
        self.messages.append((hwnd, message, wparam, lparam))
        return True

    def resolve_keyboard_target(self, hwnd: int) -> int:
        return 20 if hwnd == 10 else hwnd

    def key_message_lparam(self, virtual_key: int, *, key_up: bool) -> int:
        return virtual_key | ((1 << 31) if key_up else 0)

    def key_event(self, virtual_key: int, *, key_up: bool) -> None:
        self.key_events.append((virtual_key, key_up))

    def send_unicode_unit(self, code_unit: int, *, key_up: bool) -> None:
        self.unicode_events.append((code_unit, key_up))

    def send_mouse_wheel(self, delta: int) -> None:
        self.wheel_events.append(delta)

    def get_foreground_window(self) -> int:
        return self.foreground

    def get_cursor_pos(self) -> tuple[int, int]:
        return self.cursor

    def restore_and_focus(self, hwnd: int) -> None:
        self.foreground = hwnd
        self.focused.append(hwnd)

    def set_cursor_pos(self, x: int, y: int) -> None:
        self.cursor = (x, y)
        self.cursor_positions.append((x, y))

    def mouse_event(self, flags: int) -> None:
        self.mouse_events.append(flags)

    def send_mouse_input(self, flags: int) -> None:
        self.input_events.append(flags)


class WindowsInputTests(unittest.TestCase):
    def test_higher_integrity_target_has_actionable_error(self) -> None:
        with (
            patch(
                "automadin.engine.windows_input._window_integrity_rid",
                return_value=0x3000,
            ),
            patch(
                "automadin.engine.windows_input._current_integrity_rid",
                return_value=0x2000,
            ),
            patch("automadin.engine.windows_input.ctypes.windll") as windll,
            self.assertRaisesRegex(
                WindowsInputPrivilegeError,
                "running as administrator while Automadin is not",
            ),
        ):
            windll.user32.GetWindowTextLengthW.return_value = 4
            windll.user32.GetWindowTextW.side_effect = (
                lambda _hwnd, buffer, _length: setattr(buffer, "value", "Raid")
            )
            ensure_windows_input_access(10)

    def test_same_or_unknown_integrity_does_not_block_input(self) -> None:
        for target, current in ((0x2000, 0x2000), (None, 0x2000), (0x3000, None)):
            with (
                self.subTest(target=target, current=current),
                patch(
                    "automadin.engine.windows_input._window_integrity_rid",
                    return_value=target,
                ),
                patch(
                    "automadin.engine.windows_input._current_integrity_rid",
                    return_value=current,
                ),
            ):
                ensure_windows_input_access(10)

    def test_foreground_activation_attaches_and_detaches_foreground_queue(self) -> None:
        events: list[tuple[object, ...]] = []

        class User32:
            current_foreground = 99

            def GetForegroundWindow(self) -> int:
                return self.current_foreground

            def GetWindowThreadProcessId(self, hwnd: int, _pid) -> int:
                return {99: 22, 10: 33}[hwnd]

            def AttachThreadInput(self, source: int, target: int, attach: bool) -> bool:
                events.append(("attach", source, target, attach))
                return True

            def BringWindowToTop(self, hwnd: int) -> None:
                events.append(("raise", hwnd))

            def SetForegroundWindow(self, hwnd: int) -> None:
                events.append(("foreground", hwnd))
                self.current_foreground = hwnd

            def SetActiveWindow(self, hwnd: int) -> None:
                events.append(("active", hwnd))

            def SetFocus(self, hwnd: int) -> None:
                events.append(("focus", hwnd))

        class Kernel32:
            def GetCurrentThreadId(self) -> int:
                return 11

        user32 = User32()
        _activate_foreground_window(user32, Kernel32(), 10)

        self.assertEqual(
            events,
            [
                ("attach", 11, 22, True),
                ("attach", 11, 33, True),
                ("raise", 10),
                ("foreground", 10),
                ("active", 10),
                ("focus", 10),
                ("attach", 11, 33, False),
                ("attach", 11, 22, False),
            ],
        )

    def test_foreground_activation_uses_alt_fallback_when_windows_rejects_it(self) -> None:
        events: list[tuple[object, ...]] = []

        class User32:
            def GetForegroundWindow(self) -> int:
                return 99

            def GetWindowThreadProcessId(self, hwnd: int, _pid) -> int:
                return {99: 22, 10: 33}[hwnd]

            def AttachThreadInput(self, source: int, target: int, attach: bool) -> bool:
                return True

            def BringWindowToTop(self, hwnd: int) -> None:
                events.append(("raise", hwnd))

            def SetForegroundWindow(self, hwnd: int) -> None:
                events.append(("foreground", hwnd))

            def SetActiveWindow(self, _hwnd: int) -> None:
                pass

            def SetFocus(self, _hwnd: int) -> None:
                pass

            def keybd_event(self, key: int, scan: int, flags: int, extra: int) -> None:
                events.append(("key", key, scan, flags, extra))

        class Kernel32:
            def GetCurrentThreadId(self) -> int:
                return 11

        _activate_foreground_window(User32(), Kernel32(), 10)

        self.assertEqual(
            events[-4:],
            [
                ("key", 0x12, 0, 0, 0),
                ("key", 0x12, 0, 2, 0),
                ("raise", 10),
                ("foreground", 10),
            ],
        )

    @unittest.skipUnless(sys.platform == "win32", "Windows native backend")
    def test_native_backend_configures_against_real_user32(self) -> None:
        NativeWindowsInputBackend()

    def test_message_click_targets_child_without_focus_or_cursor_movement(self) -> None:
        backend = FakeBackend()

        click_window(10, 25, 40, mode="message", backend=backend)

        self.assertEqual(
            backend.messages,
            [
                (20, WM_MOUSEMOVE, 0, _pack_point(7, 9)),
                (20, WM_LBUTTONDOWN, 1, _pack_point(7, 9)),
                (20, WM_LBUTTONUP, 0, _pack_point(7, 9)),
                (20, WM_CANCELMODE, 0, 0),
            ],
        )
        self.assertEqual(backend.focused, [])
        self.assertEqual(backend.cursor_positions, [])
        self.assertEqual(backend.mouse_events, [])
        self.assertEqual(backend.input_events, [])

    def test_message_keys_target_selected_apps_focused_child_without_focus(self) -> None:
        backend = FakeBackend()

        press_window_keys(
            10,
            [0x11, ord("A")],
            sequence_mode="simultaneous",
            delivery_mode="message",
            backend=backend,
        )

        self.assertEqual(
            backend.messages,
            [
                (20, WM_KEYDOWN, 0x11, 0x11),
                (20, WM_KEYDOWN, ord("A"), ord("A")),
                (20, WM_KEYUP, ord("A"), ord("A") | (1 << 31)),
                (20, WM_KEYUP, 0x11, 0x11 | (1 << 31)),
            ],
        )
        self.assertEqual(backend.focused, [])
        self.assertEqual(backend.key_events, [])

    def test_combined_keys_send_character_between_key_down_and_up(self) -> None:
        backend = FakeBackend()

        press_window_keys(
            10,
            [ord("M")],
            delivery_mode="background_combined",
            backend=backend,
        )

        self.assertEqual(
            backend.messages,
            [
                (20, WM_KEYDOWN, ord("M"), ord("M")),
                (20, WM_CHAR, ord("m"), 1),
                (20, WM_KEYUP, ord("M"), ord("M") | (1 << 31)),
            ],
        )

    def test_physical_keys_focus_target_then_restore_previous_app(self) -> None:
        backend = FakeBackend()

        with patch("automadin.engine.windows_input.time.sleep") as sleep:
            press_window_keys(
                10,
                [0x26, 0x0D],
                sequence_mode="sequential",
                delivery_mode="input",
                pause_ms=25,
                focus_delay_ms=200,
                restore_delay_ms=100,
                backend=backend,
            )

        self.assertEqual(backend.focused, [10, 99])
        self.assertEqual(
            backend.key_events,
            [(0x26, False), (0x26, True), (0x0D, False), (0x0D, True)],
        )
        delays = [item.args[0] for item in sleep.call_args_list]
        self.assertIn(0.2, delays)
        self.assertIn(0.025, delays)
        self.assertIn(0.1, delays)

    def test_physical_keys_emit_nothing_when_target_cannot_be_focused(self) -> None:
        backend = FakeBackend()

        def reject_focus(hwnd: int) -> None:
            backend.focused.append(hwnd)

        backend.restore_and_focus = reject_focus  # type: ignore[method-assign]
        with (
            patch("automadin.engine.windows_input.time.sleep"),
            self.assertRaisesRegex(RuntimeError, "refused to focus"),
        ):
            press_window_keys(10, [0x0D], delivery_mode="input", backend=backend)

        self.assertEqual(backend.key_events, [])

    def test_message_text_targets_focused_child_and_preserves_unicode(self) -> None:
        backend = FakeBackend()

        type_window_text(10, "A😀", delivery_mode="message", backend=backend)

        self.assertEqual(
            backend.messages,
            [
                (20, WM_CHAR, ord("A"), 1),
                (20, WM_CHAR, 0xD83D, 1),
                (20, WM_CHAR, 0xDE00, 1),
            ],
        )
        self.assertEqual(backend.focused, [])

    def test_background_key_text_sends_key_events_without_character(self) -> None:
        backend = FakeBackend()

        type_window_text(10, "m", delivery_mode="background_key", backend=backend)

        self.assertEqual(
            backend.messages,
            [
                (20, WM_KEYDOWN, ord("M"), ord("M")),
                (20, WM_KEYUP, ord("M"), ord("M") | (1 << 31)),
            ],
        )

    def test_combined_text_sends_key_character_key_sequence(self) -> None:
        backend = FakeBackend()

        type_window_text(
            10,
            "m",
            delivery_mode="background_combined",
            backend=backend,
        )

        self.assertEqual(
            backend.messages,
            [
                (20, WM_KEYDOWN, ord("M"), ord("M")),
                (20, WM_CHAR, ord("m"), 1),
                (20, WM_KEYUP, ord("M"), ord("M") | (1 << 31)),
            ],
        )

    def test_activated_text_wraps_combined_delivery_without_foreground_focus(self) -> None:
        backend = FakeBackend()

        type_window_text(
            10,
            "m",
            delivery_mode="background_activated",
            backend=backend,
        )

        self.assertEqual(
            backend.messages,
            [
                (10, WM_ACTIVATEAPP, 1, 0),
                (10, WM_NCACTIVATE, 1, 0),
                (10, WM_ACTIVATE, 1, 0),
                (20, WM_SETFOCUS, 0, 0),
                (20, WM_KEYDOWN, ord("M"), ord("M")),
                (20, WM_CHAR, ord("m"), 1),
                (20, WM_KEYUP, ord("M"), ord("M") | (1 << 31)),
                (20, WM_KILLFOCUS, 0, 0),
                (10, WM_ACTIVATE, 0, 0),
                (10, WM_NCACTIVATE, 0, 0),
                (10, WM_ACTIVATEAPP, 0, 0),
            ],
        )
        self.assertEqual(backend.focused, [])

    def test_physical_text_focuses_types_and_restores(self) -> None:
        backend = FakeBackend()

        with patch("automadin.engine.windows_input.time.sleep") as sleep:
            type_window_text(
                10,
                "A",
                delivery_mode="input",
                min_delay_ms=25,
                max_delay_ms=25,
                focus_delay_ms=200,
                restore_delay_ms=100,
                backend=backend,
            )

        self.assertEqual(backend.focused, [10, 99])
        self.assertEqual(backend.unicode_events, [(ord("A"), False), (ord("A"), True)])
        delays = [item.args[0] for item in sleep.call_args_list]
        self.assertIn(0.2, delays)
        self.assertIn(0.025, delays)
        self.assertIn(0.1, delays)

    def test_message_scroll_targets_child_without_cursor_or_focus_change(self) -> None:
        backend = FakeBackend()

        scroll_window(10, 25, 40, -3, delivery_mode="message", backend=backend)

        self.assertEqual(
            backend.messages,
            [
                (
                    20,
                    WM_MOUSEWHEEL,
                    ((-360) & 0xFFFF) << 16,
                    _pack_point(125, 240),
                )
            ],
        )
        self.assertEqual(backend.focused, [])
        self.assertEqual(backend.cursor_positions, [])

    def test_physical_scroll_focuses_moves_scrolls_and_restores(self) -> None:
        backend = FakeBackend()

        with patch("automadin.engine.windows_input.time.sleep"):
            scroll_window(10, 25, 40, 2, delivery_mode="input", backend=backend)

        self.assertEqual(backend.focused, [10, 99])
        self.assertEqual(backend.cursor_positions, [(125, 240), (500, 600)])
        self.assertEqual(backend.wheel_events, [240])

    def test_focus_window_verifies_foreground_activation(self) -> None:
        backend = FakeBackend()

        focus_window(10, backend=backend)

        self.assertEqual(backend.focused, [10])

    def test_focus_window_reports_refused_activation(self) -> None:
        backend = FakeBackend()
        backend.restore_and_focus = lambda _hwnd: None  # type: ignore[method-assign]

        with (
            patch("automadin.engine.windows_input.time.sleep"),
            self.assertRaisesRegex(RuntimeError, "refused to focus"),
        ):
            focus_window(10, backend=backend)

    def test_message_double_click_uses_double_click_message(self) -> None:
        backend = FakeBackend()

        with patch("automadin.engine.windows_input.time.sleep"):
            click_window(10, 25, 40, click_type="double", backend=backend)

        self.assertEqual(
            [message for _, message, _, _ in backend.messages],
            [
                WM_MOUSEMOVE,
                WM_LBUTTONDOWN,
                WM_LBUTTONUP,
                WM_LBUTTONDBLCLK,
                WM_LBUTTONUP,
                WM_CANCELMODE,
            ],
        )

    def test_message_click_cancels_capture_when_button_up_is_rejected(self) -> None:
        backend = FakeBackend()

        def reject_button_up(
            hwnd: int, message: int, wparam: int, lparam: int
        ) -> bool:
            backend.messages.append((hwnd, message, wparam, lparam))
            return message != WM_LBUTTONUP

        backend.post_message = reject_button_up  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "0x0202 was rejected"):
            click_window(10, 25, 40, mode="message", backend=backend)

        self.assertEqual(backend.messages[-1], (20, WM_CANCELMODE, 0, 0))

    def test_physical_modes_restore_previous_cursor_and_foreground(self) -> None:
        for mode, event_attribute in [
            ("cursor", "mouse_events"),
            ("input", "input_events"),
        ]:
            with self.subTest(mode=mode):
                backend = FakeBackend()

                with patch("automadin.engine.windows_input.time.sleep"):
                    click_window(10, 25, 40, mode=mode, backend=backend)

                self.assertEqual(backend.focused, [10, 99])
                self.assertEqual(
                    backend.cursor_positions, [(125, 240), (500, 600)]
                )
                self.assertEqual(
                    getattr(backend, event_attribute),
                    [MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP],
                )

    def test_physical_mode_waits_for_focus_press_and_restore(self) -> None:
        backend = FakeBackend()

        with patch("automadin.engine.windows_input.time.sleep") as sleep:
            click_window(
                10,
                25,
                40,
                mode="input",
                focus_delay_ms=250,
                click_duration_ms=60,
                restore_delay_ms=120,
                backend=backend,
            )

        delays = [call.args[0] for call in sleep.call_args_list]
        self.assertIn(0.25, delays)
        self.assertIn(0.06, delays)
        self.assertIn(0.12, delays)

    def test_focused_input_mode_keeps_cursor_stable_after_release(self) -> None:
        backend = FakeBackend()
        backend.foreground = 10

        with patch("automadin.engine.windows_input.time.sleep") as sleep:
            click_window(
                10,
                25,
                40,
                mode="input",
                focus_delay_ms=250,
                click_duration_ms=60,
                restore_delay_ms=120,
                backend=backend,
            )

        delays = [call.args[0] for call in sleep.call_args_list]
        self.assertNotIn(0.25, delays)
        self.assertIn(0.06, delays)
        self.assertIn(0.12, delays)
        self.assertEqual(backend.cursor_positions, [(125, 240), (500, 600)])
        self.assertEqual(backend.focused, [10])

    def test_physical_mode_does_not_override_user_cursor_movement(self) -> None:
        backend = FakeBackend()

        def move_cursor_after_click(_flags: int) -> None:
            backend.cursor = (700, 800)

        backend.mouse_event = move_cursor_after_click  # type: ignore[method-assign]
        with patch("automadin.engine.windows_input.time.sleep"):
            click_window(10, 25, 40, mode="cursor", backend=backend)

        self.assertEqual(backend.cursor_positions, [(125, 240)])

    def test_physical_mode_does_not_override_new_user_focus(self) -> None:
        backend = FakeBackend()

        def switch_focus_after_click(_flags: int) -> None:
            backend.foreground = 77

        backend.mouse_event = switch_focus_after_click  # type: ignore[method-assign]
        with patch("automadin.engine.windows_input.time.sleep"):
            click_window(10, 25, 40, mode="cursor", backend=backend)

        self.assertEqual(backend.focused, [10])

    def test_physical_mode_does_not_click_when_focus_is_denied(self) -> None:
        backend = FakeBackend()

        def reject_focus(hwnd: int) -> None:
            backend.focused.append(hwnd)

        backend.restore_and_focus = reject_focus  # type: ignore[method-assign]
        with (
            patch("automadin.engine.windows_input.time.sleep"),
            self.assertRaisesRegex(RuntimeError, "refused to focus"),
        ):
            click_window(10, 25, 40, mode="cursor", backend=backend)

        self.assertEqual(backend.cursor_positions, [])
        self.assertEqual(backend.mouse_events, [])

    def test_unknown_mode_fails_instead_of_silently_using_physical_input(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Unsupported Windows click mode"):
            click_window(10, 25, 40, mode="unknown", backend=FakeBackend())

    def test_message_drag_posts_held_moves_without_focus_or_cursor_movement(self) -> None:
        backend = FakeBackend()

        with patch("automadin.engine.windows_input.time.sleep"):
            drag_window(
                10,
                25,
                40,
                55,
                20,
                mode="message",
                button="left",
                steps=3,
                backend=backend,
            )

        self.assertEqual(
            backend.messages,
            [
                (20, WM_MOUSEMOVE, 0, _pack_point(7, 9)),
                (20, WM_LBUTTONDOWN, 1, _pack_point(7, 9)),
                (20, WM_MOUSEMOVE, 1, _pack_point(17, 3)),
                (20, WM_MOUSEMOVE, 1, _pack_point(27, -4)),
                (20, WM_MOUSEMOVE, 1, _pack_point(37, -11)),
                (20, WM_LBUTTONUP, 0, _pack_point(37, -11)),
                (20, WM_CANCELMODE, 0, 0),
            ],
        )
        self.assertEqual(backend.focused, [])
        self.assertEqual(backend.cursor_positions, [])
        self.assertEqual(backend.mouse_events, [])
        self.assertEqual(backend.input_events, [])

    def test_physical_drag_moves_while_pressed_and_restores(self) -> None:
        backend = FakeBackend()

        with patch("automadin.engine.windows_input.time.sleep"):
            drag_window(
                10,
                25,
                40,
                55,
                20,
                mode="cursor",
                button="left",
                steps=3,
                backend=backend,
            )

        self.assertEqual(backend.focused, [10, 99])
        self.assertEqual(
            backend.cursor_positions,
            [(125, 240), (135, 234), (145, 227), (155, 220), (500, 600)],
        )
        self.assertEqual(
            backend.mouse_events,
            [
                MOUSEEVENTF_LEFTDOWN,
                MOUSEEVENTF_MOVE,
                MOUSEEVENTF_MOVE,
                MOUSEEVENTF_MOVE,
                MOUSEEVENTF_LEFTUP,
            ],
        )
