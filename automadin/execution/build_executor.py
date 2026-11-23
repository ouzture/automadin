"""Shared factory for creating a FlowExecutor with all handlers registered."""

from __future__ import annotations

from automadin.execution.executor import FlowExecutor


def build_executor(db=None) -> FlowExecutor:
    """Create a FlowExecutor with all handlers registered."""
    from automadin.execution.handlers.click import handle_click_position
    from automadin.execution.handlers.type_text import (
        handle_type_text,
        handle_clear_text_field,
    )
    from automadin.execution.handlers.navigate import (
        handle_navigate_to,
        handle_go_back,
        handle_go_forward,
        handle_reload_page,
    )
    from automadin.execution.handlers.tab import (
        handle_open_new_tab,
        handle_close_tab,
        handle_ensure_browser_page,
        handle_switch_tab,
        handle_select_browser_page,
    )
    from automadin.execution.handlers.wait import (
        handle_wait,
        handle_random_wait,
    )
    from automadin.execution.handlers.notifications import handle_send_notification
    from automadin.execution.handlers.detection import (
        handle_find_image,
        handle_read_screen_text,
        handle_screenshot,
    )
    from automadin.execution.handlers.control import (
        handle_group,
        handle_user_checkpoint,
        handle_section,
        handle_for,
        handle_while,
        handle_if_image,
        handle_if_value,
        handle_switch_value,
        handle_switch_branch_outside,
        handle_while_value,
        handle_if_elapsed,
        handle_for_each,
        handle_terminate_flow,
        handle_restart_flow,
        handle_run_flow,
    )
    from automadin.execution.handlers.variables import handle_update_variable
    from automadin.execution.handlers.browser_ext import (
        handle_evaluate_js,
        handle_assert_url_contains,
        handle_close_all_tabs,
        handle_wait_for_navigation,
    )
    from automadin.execution.handlers.browser_elements import (
        handle_assert_visible,
        handle_click_element,
        handle_extract_text,
        handle_for_each_browser_element,
        handle_input_fill,
        handle_scroll_browser,
        handle_wait_for_selector,
    )
    from automadin.execution.handlers.actions import (
        handle_click_image,
        handle_swipe,
        handle_press_keycode,
    )
    from automadin.execution.handlers.android_apps import (
        handle_android_force_stop_app,
        handle_android_install_apk,
        handle_android_launch_app,
        handle_android_open_deep_link,
        handle_android_restart_app,
        handle_android_start_activity,
    )
    from automadin.execution.handlers.visual import (
        handle_click_best_target,
        handle_click_dynamic_target,
    )
    from automadin.execution.handlers.windows import (
        handle_win_click,
        handle_win_drag,
        handle_win_key,
        handle_win_type,
        handle_win_scroll,
        handle_win_focus,
        handle_win_ensure_app,
        handle_win_click_image,
        handle_win_drag_image,
        handle_win_if_image,
        handle_win_while,
    )
    from automadin.execution.hooks import handle_check_hooks
    from automadin.execution.handlers.system_power import handle_hibernate_computer

    # Standard executors fail closed if a caller bypasses the UI preflight.
    # Custom/test executors can still opt into their own command registry by
    # constructing FlowExecutor directly (its default remains non-enforcing).
    executor = FlowExecutor(db=db, enforce_preflight=True)

    # ACTIONS
    executor.register_handler("click_position", handle_click_position)
    executor.register_handler("click_image", handle_click_image)
    executor.register_handler("type_text", handle_type_text)
    executor.register_handler("clear_text_field", handle_clear_text_field)
    executor.register_handler("swipe", handle_swipe)
    executor.register_handler("press_keycode", handle_press_keycode)
    executor.register_handler("input_fill", handle_input_fill)
    executor.register_handler("click_best_target", handle_click_best_target)
    executor.register_handler("click_dynamic_target", handle_click_dynamic_target)

    # DETECTION
    executor.register_handler("screenshot", handle_screenshot)
    executor.register_handler("read_screen_text", handle_read_screen_text)
    executor.register_handler("find_image", handle_find_image)

    # CONTROL FLOW
    executor.register_handler("section", handle_section)
    executor.register_handler("group", handle_group)
    executor.register_handler("user_checkpoint", handle_user_checkpoint)
    executor.register_handler("for", handle_for)
    executor.register_handler("while", handle_while)
    executor.register_handler("if_image", handle_if_image)
    executor.register_handler("if_value", handle_if_value)
    executor.register_handler("switch_value", handle_switch_value)
    executor.register_handler("switch_case", handle_switch_branch_outside)
    executor.register_handler("switch_default", handle_switch_branch_outside)
    executor.register_handler("while_value", handle_while_value)
    executor.register_handler("if_elapsed", handle_if_elapsed)
    executor.register_handler("for_each", handle_for_each)
    executor.register_handler("for_each_browser_element", handle_for_each_browser_element)
    executor.register_handler("terminate_flow", handle_terminate_flow)
    executor.register_handler("restart_flow", handle_restart_flow)
    executor.register_handler("run_flow", handle_run_flow)
    executor.register_handler("check_hooks", handle_check_hooks)
    executor.register_handler("update_variable", handle_update_variable)

    # NOTIFICATIONS
    executor.register_handler("send_notification", handle_send_notification)

    # LOCAL SYSTEM
    executor.register_handler("hibernate_computer", handle_hibernate_computer)

    # TIMING
    executor.register_handler("wait", handle_wait)
    executor.register_handler("random_wait", handle_random_wait)

    # BROWSER
    executor.register_handler("click_element", handle_click_element)
    executor.register_handler("navigate_to", handle_navigate_to)
    executor.register_handler("go_back", handle_go_back)
    executor.register_handler("go_forward", handle_go_forward)
    executor.register_handler("reload_page", handle_reload_page)
    executor.register_handler("open_new_tab", handle_open_new_tab)
    executor.register_handler("close_tab", handle_close_tab)
    executor.register_handler("close_all_tabs", handle_close_all_tabs)
    executor.register_handler("switch_tab", handle_switch_tab)
    executor.register_handler("select_browser_page", handle_select_browser_page)
    executor.register_handler("ensure_browser_page", handle_ensure_browser_page)
    executor.register_handler("wait_for_selector", handle_wait_for_selector)
    executor.register_handler("scroll_browser", handle_scroll_browser)
    executor.register_handler("wait_for_navigation", handle_wait_for_navigation)
    executor.register_handler("extract_text", handle_extract_text)
    executor.register_handler("evaluate_js", handle_evaluate_js)
    executor.register_handler("assert_visible", handle_assert_visible)
    executor.register_handler("assert_url_contains", handle_assert_url_contains)

    # ANDROID
    executor.register_handler("android_install_apk", handle_android_install_apk)
    executor.register_handler("android_launch_app", handle_android_launch_app)
    executor.register_handler("android_force_stop_app", handle_android_force_stop_app)
    executor.register_handler("android_restart_app", handle_android_restart_app)
    executor.register_handler("android_start_activity", handle_android_start_activity)
    executor.register_handler("android_open_deep_link", handle_android_open_deep_link)

    # WINDOWS
    executor.register_handler("win_ensure_app", handle_win_ensure_app)
    executor.register_handler("win_click", handle_win_click)
    executor.register_handler("win_drag", handle_win_drag)
    executor.register_handler("win_key", handle_win_key)
    executor.register_handler("win_type", handle_win_type)
    executor.register_handler("win_scroll", handle_win_scroll)
    executor.register_handler("win_focus", handle_win_focus)
    executor.register_handler("win_click_image", handle_win_click_image)
    executor.register_handler("win_drag_image", handle_win_drag_image)
    executor.register_handler("win_if_image", handle_win_if_image)
    executor.register_handler("win_while", handle_win_while)

    from automadin.execution.handlers.registry import load_command_types

    defined_ids = frozenset(load_command_types())
    if defined_ids != executor.registered_handler_ids:
        missing = sorted(defined_ids - executor.registered_handler_ids)
        extra = sorted(executor.registered_handler_ids - defined_ids)
        raise RuntimeError(
            f"Command registry mismatch; missing handlers={missing}, extra handlers={extra}"
        )

    return executor
