from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

import pytest

from automadin.app import _browser_companion_host_requested
from automadin.engine.browser_engine import BrowserDeviceEngine
from automadin.models.device import BrowserDeviceConfig, BrowserEngine
from automadin.services.browser_companion_broker import BrowserCompanionBroker
from automadin.services.browser_companion_broker import CompanionTarget


def test_companion_device_id_round_trip_and_display() -> None:
    config = BrowserDeviceConfig(
        engine=BrowserEngine.CHROME,
        companion_client_id="client-1",
        companion_target_id="4:22",
        companion_title="Checkout",
        companion_url="https://example.test/checkout",
    )

    restored = BrowserDeviceConfig.from_id(config.to_id())

    assert restored == config
    assert restored.to_display_name() == "Browser Companion · Chrome · Checkout"


def test_companion_is_identified_and_listed_before_launch_targets(monkeypatch) -> None:
    from automadin.services import browser_companion_broker

    target = CompanionTarget(
        "client-1",
        "4:22",
        "Test centre",
        "https://example.test",
        0,
        "msedge",
    )
    monkeypatch.setattr(
        browser_companion_broker,
        "browser_companion_broker",
        lambda: type("Broker", (), {"targets": lambda self: [target]})(),
    )

    devices = BrowserDeviceEngine().generate_devices()

    assert devices[0].config.companion_target_id == "4:22"
    assert devices[0].config.engine is BrowserEngine.MSEDGE
    assert devices[0].name == "Browser Companion · MS Edge · Test centre"


def test_native_host_launch_is_detected_from_registered_extension(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "Automadin.exe",
            "chrome-extension://mngmmchlajnjfakibmioediekahneckp/",
            "--parent-window=42",
        ],
    )

    assert _browser_companion_host_requested()


def test_native_host_socket_timeout_only_applies_while_connecting(monkeypatch) -> None:
    from automadin import browser_companion_host

    class Connection:
        timeout = object()

        def settimeout(self, value):
            self.timeout = value

    connection = Connection()

    def create_connection(address, *, timeout):
        assert address == ("127.0.0.1", 45211)
        assert timeout == 5
        return connection

    monkeypatch.setattr(browser_companion_host.socket, "create_connection", create_connection)

    assert browser_companion_host._connect_to_broker({"port": 45211}) is connection
    assert connection.timeout is None


def test_broker_publishes_only_explicitly_granted_target(tmp_path: Path) -> None:
    broker = BrowserCompanionBroker(tmp_path)
    broker.start()
    descriptor = json.loads(broker.descriptor_path.read_text(encoding="utf-8"))
    client = socket.create_connection(("127.0.0.1", descriptor["port"]), timeout=2)
    stream = client.makefile("rw", encoding="utf-8", newline="\n")
    stream.write(json.dumps({"type": "auth", "token": descriptor["token"]}) + "\n")
    stream.write(json.dumps({"type": "host_hello", "parent_window": 42}) + "\n")
    stream.write(
        json.dumps({"type": "hello", "user_agent": "Chrome/140.0 Edg/140.0"}) + "\n"
    )
    stream.write(
        json.dumps(
            {
                "type": "granted",
                "target_id": "7:9",
                "title": "Orders",
                "url": "https://example.test/orders",
            }
        )
        + "\n"
    )
    stream.flush()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not broker.targets():
            time.sleep(0.01)
        assert broker.targets()[0].target_id == "7:9"
        assert broker.targets()[0].parent_window == 42
        assert broker.targets()[0].engine == "msedge"
    finally:
        client.close()
        broker.close()


def test_extension_and_native_manifest_use_same_fixed_identity() -> None:
    resources = Path(__file__).parents[1] / "automadin" / "resources"
    extension = json.loads(
        (resources / "browser_companion_extension" / "manifest.json").read_text(encoding="utf-8")
    )
    host = json.loads(
        (resources / "browser_companion_native_host.json").read_text(encoding="utf-8")
    )

    assert extension["manifest_version"] == 3
    assert "storage" in extension["permissions"]
    assert "userScripts" in extension["permissions"]
    assert extension["host_permissions"] == ["http://*/*", "https://*/*"]
    assert host["name"] == "com.automadin.browser_companion"
    assert host["allowed_origins"] == ["chrome-extension://mngmmchlajnjfakibmioediekahneckp/"]


def test_extension_persists_and_restores_explicit_tab_grant() -> None:
    worker = (
        Path(__file__).parents[1]
        / "automadin"
        / "resources"
        / "browser_companion_extension"
        / "service_worker.js"
    ).read_text(encoding="utf-8")

    assert 'chrome.storage.session.set({granted: value})' in worker
    assert 'chrome.storage.session.get("granted")' in worker
    assert 'chrome.action.getBadgeText({tabId: tab.id}) === "ON"' in worker
    assert "await restoreGrant();" in worker
    assert 'request.op === "load_status"' in worker
    assert "completedLoads.set(tabId" in worker
    assert "new URL(change.url).origin" not in worker
    assert "Companion could not access the loaded page" in worker
    assert 'setBadgeText({tabId: tab.id, text: ""})' in worker
    assert "setTimeout(connect, 2000)" not in worker
    assert 'request.op === "evaluate"' in worker
    assert "chrome.userScripts.execute" in worker
    assert 'world: "MAIN"' in worker
    assert 'protocol: 4' in worker
    assert "CONTENT_REPLY_TIMEOUT_MS = 3000" in worker
    assert "MAX_CONTENT_REPLY_TIMEOUT_MS = 120000" in worker
    assert "contentReplyTimeout(request)" in worker
    assert 'request.op === "fill"' in worker
    assert "String(p.value ?? \"\").length" in worker
    assert 'request.op === "type"' in worker
    assert 'request.op === "press"' in worker
    assert 'request.op === "click"' in worker
    assert 'if (request.op !== "visible") throw error;' in worker
    assert 'files: ["content.js"]' in worker
    assert "response.result = await requestContent(" in worker


def test_evaluate_js_is_not_run_through_content_script_eval() -> None:
    content = (
        Path(__file__).parents[1]
        / "automadin"
        / "resources"
        / "browser_companion_extension"
        / "content.js"
    ).read_text(encoding="utf-8")

    assert 'request.op==="evaluate"' not in content
    assert "eval)(p.script)" not in content
    assert 'request.op==="click"' in content
    assert 'request.op==="click_position"' in content
    assert 'request.op==="fill"' in content
    assert 'request.op==="type"' in content
    assert 'request.op==="press"' in content
    assert 'new InputEvent("input"' in content
    assert 'e.type!=="radio"' in content
    assert "group.radios.find(e=>e.checked)||group.radios[0]" in content
    assert "radioStops.has(e)" in content


def test_outdated_companion_protocol_gets_actionable_evaluate_error(tmp_path: Path) -> None:
    broker = BrowserCompanionBroker(tmp_path)
    broker.start()
    descriptor = json.loads(broker.descriptor_path.read_text(encoding="utf-8"))
    client = socket.create_connection(("127.0.0.1", descriptor["port"]), timeout=2)
    stream = client.makefile("rw", encoding="utf-8", newline="\n")
    stream.write(json.dumps({"type": "auth", "token": descriptor["token"]}) + "\n")
    stream.write(json.dumps({"type": "hello", "protocol": 1}) + "\n")
    stream.write(
        json.dumps(
            {
                "type": "granted",
                "target_id": "7:9",
                "title": "Orders",
                "url": "https://example.test/orders",
            }
        )
        + "\n"
    )
    stream.flush()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not broker.targets():
            time.sleep(0.01)
        target = broker.targets()[0]
        with pytest.raises(RuntimeError, match="extension is outdated") as caught:
            broker.request(target.client_id, "evaluate", {"script": "1 + 1"})
        assert "Allow User Scripts" in str(caught.value)
        assert "click Reload" in str(caught.value)
    finally:
        client.close()
        broker.close()


def test_previous_companion_protocol_gets_actionable_dom_input_error(tmp_path: Path) -> None:
    broker = BrowserCompanionBroker(tmp_path)
    broker.start()
    descriptor = json.loads(broker.descriptor_path.read_text(encoding="utf-8"))
    client = socket.create_connection(("127.0.0.1", descriptor["port"]), timeout=2)
    stream = client.makefile("rw", encoding="utf-8", newline="\n")
    stream.write(json.dumps({"type": "auth", "token": descriptor["token"]}) + "\n")
    stream.write(json.dumps({"type": "hello", "protocol": 2}) + "\n")
    stream.flush()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not broker._clients:
            time.sleep(0.01)
        client_id = next(iter(broker._clients))
        with pytest.raises(RuntimeError, match="extension is outdated"):
            broker.request(client_id, "click", {"locator": "#save"})
    finally:
        client.close()
        broker.close()


def test_companion_click_and_text_use_extension_operations() -> None:
    from automadin.engine.browser_companion import CompanionBrowserPort

    class Broker:
        def __init__(self):
            self.calls = []

        def request(self, _client_id, operation, params):
            self.calls.append((operation, params))
            if operation == "visible":
                return True
            return None

    broker = Broker()
    target = CompanionTarget("client", "7:9", "Orders", "https://example.test", 0)
    port = CompanionBrowserPort(broker, target)

    port.click_locator("#save", anchor="bottom", offset_y=-2)
    port.fill_locator("#name", "Automadin", min_delay=10, max_delay=20)
    port.type_text("!", min_delay_ms=5, max_delay_ms=8)
    port.press_keys(["Tab", "Space", "Enter"], mode="sequential", pause_ms=100)
    port.click_position(25, 40, click_type="double")

    assert broker.calls == [
        ("visible", {"locator": "#save"}),
        (
            "click",
            {
                "locator": "#save",
                "anchor": "bottom",
                "offset_x": 0.0,
                "offset_y": -2.0,
                "offset_unit": "px",
                "click_type": "single",
                "hold_duration_ms": 0,
            },
        ),
        ("visible", {"locator": "#name"}),
        (
            "fill",
            {
                "locator": "#name",
                "value": "Automadin",
                "min_delay": 10,
                "max_delay": 20,
            },
        ),
        ("type", {"text": "!", "min_delay_ms": 5, "max_delay_ms": 8}),
        (
            "press",
            {"keys": ["Tab", "Space", "Enter"], "mode": "sequential", "pause_ms": 100},
        ),
        (
            "click_position",
            {"x": 25, "y": 40, "click_type": "double", "hold_duration_ms": 0},
        ),
    ]


def test_broker_close_disconnects_native_host_client(tmp_path: Path) -> None:
    broker = BrowserCompanionBroker(tmp_path)
    broker.start()
    descriptor = json.loads(broker.descriptor_path.read_text(encoding="utf-8"))
    client = socket.create_connection(("127.0.0.1", descriptor["port"]), timeout=2)
    stream = client.makefile("rw", encoding="utf-8", newline="\n")
    stream.write(json.dumps({"type": "auth", "token": descriptor["token"]}) + "\n")
    stream.flush()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not broker._clients:
        time.sleep(0.01)

    broker.close()
    client.settimeout(2)
    assert client.recv(1) == b""


def test_zero_parent_window_finds_unique_browser_window_without_foreground_requirement(
    monkeypatch,
) -> None:
    from automadin.engine import browser_companion

    class Broker:
        def request(self, _client_id, operation, _params):
            assert operation == "tab_info"
            return {"active": True, "title": "Orders"}

    monkeypatch.setattr(browser_companion, "_matching_browser_windows", lambda title: [42])
    target = CompanionTarget("client", "7:9", "Orders", "https://example.test", 0)

    assert browser_companion.CompanionBrowserPort(Broker(), target)._browser_window() == 42


def test_zero_parent_window_rejects_ambiguous_browser_windows(monkeypatch) -> None:
    from automadin.engine import browser_companion

    class Broker:
        def request(self, _client_id, _operation, _params):
            return {"active": True, "title": "Orders"}

    monkeypatch.setattr(browser_companion, "_matching_browser_windows", lambda title: [42, 43])
    target = CompanionTarget("client", "7:9", "Orders", "https://example.test", 0)

    with pytest.raises(RuntimeError, match="uniquely locate"):
        browser_companion.CompanionBrowserPort(Broker(), target)._browser_window()


def test_companion_navigation_waits_for_matching_completed_load_epoch() -> None:
    from automadin.engine.browser_companion import CompanionBrowserPort

    class Broker:
        def __init__(self):
            self.calls = []
            self.statuses = iter((False, True))

        def request(self, _client_id, operation, params):
            self.calls.append((operation, params))
            if operation == "navigate":
                return 7
            if operation == "load_status":
                return {"complete": next(self.statuses)}
            if operation == "viewport":
                return {"width": 100, "height": 100}
            raise AssertionError(operation)

    broker = Broker()
    target = CompanionTarget("client", "7:9", "Orders", "https://example.test", 0)
    port = CompanionBrowserPort(broker, target)

    port.navigate_to("https://example.test/next")
    port.wait_for_load_state("domcontentloaded", timeout_ms=500)

    assert broker.calls[0] == ("navigate", {"url": "https://example.test/next"})
    assert broker.calls[1:3] == [
        ("load_status", {"epoch": 7}),
        ("load_status", {"epoch": 7}),
    ]
    assert broker.calls[3] == ("viewport", {})


def test_companion_load_permission_failure_is_not_retried_until_timeout() -> None:
    from automadin.engine.browser_companion import CompanionBrowserPort

    class Broker:
        calls = 0

        def request(self, _client_id, operation, _params):
            assert operation == "load_status"
            self.calls += 1
            raise RuntimeError("Companion could not access the loaded page")

    broker = Broker()
    target = CompanionTarget("client", "7:9", "Orders", "https://example.test", 0)

    with pytest.raises(RuntimeError, match="could not access"):
        CompanionBrowserPort(broker, target).wait_for_load_state(
            "domcontentloaded", timeout_ms=500
        )

    assert broker.calls == 1


def test_companion_visibility_is_false_while_document_receiver_is_replaced() -> None:
    from automadin.engine.browser_companion import CompanionBrowserPort

    class Broker:
        def request(self, _client_id, operation, _params):
            assert operation == "visible"
            raise RuntimeError("Could not establish connection. Receiving end does not exist.")

    target = CompanionTarget("client", "7:9", "Orders", "https://example.test", 0)

    assert not CompanionBrowserPort(Broker(), target).is_locator_visible("#blocked")


def test_companion_visibility_does_not_hide_a_lost_grant() -> None:
    from automadin.engine.browser_companion import CompanionBrowserPort

    class Broker:
        def request(self, _client_id, operation, _params):
            assert operation == "visible"
            raise RuntimeError("No browser tab is granted to Automadin")

    target = CompanionTarget("client", "7:9", "Orders", "https://example.test", 0)

    with pytest.raises(RuntimeError, match="No browser tab"):
        CompanionBrowserPort(Broker(), target).is_locator_visible("#blocked")
