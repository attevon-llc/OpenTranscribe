"""GPU telemetry is delivered to administrator sockets only."""

import json
from unittest.mock import AsyncMock

import pytest

from app.api import websockets as ws


def _sock():
    s = AsyncMock()
    return s


@pytest.fixture
def mgr(monkeypatch):
    m = ws.ConnectionManager()
    monkeypatch.setattr(ws, "manager", m)
    return m


def _sent(sock):
    return [json.loads(c.args[0]) for c in sock.send_text.call_args_list]


async def _dispatch(payload: dict):
    await ws._dispatch_notification({"type": "message", "data": json.dumps(payload)})


@pytest.mark.asyncio
async def test_non_admin_socket_receives_no_gpu_telemetry(mgr):
    admin, user = _sock(), _sock()
    mgr.connect(admin, 1, host_telemetry=True)
    mgr.connect(user, 2)
    await _dispatch(
        {"type": "gpu_stats_update", "broadcast": True, "admin_only": True, "data": [{"x": 1}]}
    )
    assert [m["type"] for m in _sent(admin)] == ["gpu_stats_update"]
    assert _sent(user) == []


@pytest.mark.asyncio
async def test_gpu_event_without_admin_flag_still_not_sent_to_users(mgr):
    user = _sock()
    mgr.connect(user, 2)
    await _dispatch({"type": "gpu_stats_update", "broadcast": True, "data": []})
    assert _sent(user) == []


@pytest.mark.asyncio
async def test_other_broadcasts_still_reach_everyone(mgr):
    admin, user = _sock(), _sock()
    mgr.connect(admin, 1, host_telemetry=True)
    mgr.connect(user, 2)
    await _dispatch({"type": "something_else", "broadcast": True, "data": {}})
    assert len(_sent(admin)) == 1
    assert len(_sent(user)) == 1


@pytest.mark.asyncio
async def test_disconnect_removes_admin_socket(mgr):
    admin = _sock()
    mgr.connect(admin, 1, host_telemetry=True)
    mgr.disconnect(admin, 1)
    await mgr.broadcast_to_admins({"type": "gpu_stats_update", "data": []})
    assert _sent(admin) == []


def test_publisher_marks_gpu_stats_admin_only():
    import inspect

    from app.tasks import utility

    src = inspect.getsource(utility.update_gpu_stats)
    assert '"admin_only": True' in src


class _U:
    def __init__(self, role: str, is_superuser: bool = False) -> None:
        self.role = role
        self.is_superuser = is_superuser


def test_host_telemetry_follows_the_hardware_stats_capability(monkeypatch):
    """Community: admins get it. Tenant-hidden capability: only platform staff do."""
    from app.api import websockets as ws

    monkeypatch.setattr(ws, "capability_enabled", lambda key, request=None: True)
    assert ws._may_receive_host_telemetry(_U("admin")) is True
    assert ws._may_receive_host_telemetry(_U("super_admin")) is True
    assert ws._may_receive_host_telemetry(_U("user")) is False

    monkeypatch.setattr(ws, "capability_enabled", lambda key, request=None: False)
    assert ws._may_receive_host_telemetry(_U("admin")) is False, "a tenant's own admin"
    assert ws._may_receive_host_telemetry(_U("super_admin")) is False
    assert ws._may_receive_host_telemetry(_U("super_admin", is_superuser=True)) is True
