"""The assert-inert checker (issue #898): its three parts, each with a must-fire case.

* The publish counter: a publish through ``before_task_publish`` is counted, and with the
  receiver absent the checker says NOT MEASURED (exit 3), never a quiet zero.
* The route model: every GET route that enqueues work today (``GET_ROUTES_THAT_ENQUEUE``) is
  found on the real app with a call chain, a WebSocket route is modelled as one, an unknown
  route type fails closed, and a route that enqueues nothing stays clean.
* The beat plane: an entry with no ``BeatEffect`` (or a stale one) is reported. That every
  real entry has one is ``test_beat_entry_effects.py``.

``GET_ROUTES_THAT_ENQUEUE`` is a ratchet. A new safe-method route that reaches a publish fails
``test_the_real_app_enqueues_on_exactly_the_known_get_routes``; a route made inert must leave
the fixture in the same change. The public demo (#628) needs it empty.
"""

from __future__ import annotations

import importlib

import pytest
from celery import signals
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import inert_routes
from app.core import inert
from app.core.celery_beat import BEAT_ENTRY_EFFECT
from app.core.celery_beat import BEAT_SCHEDULE
from app.core.celery_beat import BeatEffect
from app.core.inert_reach import ReachabilityModel

#: Every safe-method route of the real app that reaches a Celery publish, with where.
GET_ROUTES_THAT_ENQUEUE: dict[str, str] = {
    "GET /api/files/{file_uuid}": (
        "redaction_gate.py: lazy redaction scan via the detail body; detail_response.py: "
        "queue missing analytics (skipped in DEMO_MODE)"
    ),
    "GET /api/files/{file_uuid}/analytics": (
        "detail_response.py: queue missing analytics (skipped in DEMO_MODE)"
    ),
    "GET /api/files/{file_uuid}/segments": "redaction_gate.py: lazy redaction scan",
    "GET /api/files/{file_uuid}/subtitles": "redaction_gate.py: lazy redaction scan",
    "GET /api/files/{file_uuid}/captions": "redaction_gate.py: lazy redaction scan",
    "GET /api/files/{file_uuid}/export": "redaction_gate.py: lazy redaction scan",
    "GET /api/files/{file_uuid}/download-stream": "media_delivery.py: prepare_media_download",
    "GET /api/admin/stats": "admin_system_metrics.py: on-demand system.update_gpu_stats",
    "GET /api/system/stats": "admin_system_metrics.py: on-demand system.update_gpu_stats",
    "GET /api/auth/oidc/callback": "group_file_index_service.py: access reindex on group sync",
    "GET /api/speaker-profiles/speakers/{speaker_uuid}/suggestions": (
        "repair.py: index self-heal after a failed kNN read (skipped in DEMO_MODE)"
    ),
}

#: Safe-method routes that must stay clean: if one of these is flagged, the model over-reaches.
ROUTES_THAT_MUST_STAY_CLEAN = (
    "GET /api/auth/session",
    "GET /api/files/{file_uuid}/thumbnail",
    "WEBSOCKET /api/ws",
)


#: The route model walks only these packages; this module holds the synthetic routes.
PACKAGES = ("app", __name__)


#: The synthetic apps have 3 routes; the production floor would (correctly) call them unmeasured.
SMALL_APP = ["--min-routes", "3"]


def _model():
    return ReachabilityModel(packages=PACKAGES)


def _helper_that_enqueues(task):
    task.delay(1)


def _helper_that_does_not(value):
    return value + 1


class EnqueuingHttpMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        _helper_that_enqueues(None)
        return await call_next(request)


@pytest.fixture(scope="module")
def live_app():
    importlib.import_module("app.core.celery")  # connects the before_task_publish receiver
    from app.main import app

    return app


@pytest.fixture(scope="module")
def live_findings(live_app):
    return inert_routes.model_routes(list(live_app.routes))


@pytest.fixture
def hook_absent():
    from app.core.celery import inject_request_id_header

    signals.before_task_publish.disconnect(inject_request_id_header)
    try:
        yield
    finally:
        signals.before_task_publish.connect(inject_request_id_header)


def _quiet_read():
    return {"value": _helper_that_does_not(1)}


def _noisy_read():
    _helper_that_enqueues(None)
    return {}


async def _quiet_socket(websocket):
    await websocket.close()


async def _noisy_socket(websocket):
    _helper_that_enqueues(None)
    await websocket.close()


def _synthetic_app(enqueue_on_get: bool = False, enqueue_on_ws: bool = False):
    """GET /read, WEBSOCKET /socket and POST /write; the model is flow-insensitive, so the
    flags choose which endpoint function is registered rather than branching inside one."""
    from fastapi import FastAPI

    app = FastAPI()
    app.add_api_route("/read", _noisy_read if enqueue_on_get else _quiet_read, methods=["GET"])
    app.add_api_websocket_route("/socket", _noisy_socket if enqueue_on_ws else _quiet_socket)
    app.add_api_route("/write", _noisy_read, methods=["POST"])
    return app


# --- the publish counter -----------------------------------------------------------------


class TestPublishCounter:
    def test_a_publish_through_the_signal_is_counted_by_task(self, live_app):
        before = inert.published_counts().get("demo.inert.count", 0.0)

        signals.before_task_publish.send(sender="demo.inert.count", headers={}, body=None)

        assert inert.published_counts()["demo.inert.count"] == before + 1

    def test_the_window_reports_publishes_made_inside_it_and_excludes_the_probe(self, live_app):
        with inert.PublishWindow() as window:
            signals.before_task_publish.send(sender="demo.inert.window", headers={}, body=None)

        assert window.result.measured is True
        assert window.result.published == {"demo.inert.window": 1.0}
        assert window.result.status == inert.Status.NOT_INERT

    def test_an_empty_window_is_inert_only_when_measured(self, live_app):
        with inert.PublishWindow() as window:
            pass

        assert window.result.status == inert.Status.INERT
        assert window.result.total == 0

    def test_without_the_receiver_the_window_is_not_measured(self, live_app, hook_absent):
        with inert.PublishWindow() as window:
            signals.before_task_publish.send(sender="demo.inert.lost", headers={}, body=None)

        assert window.result.measured is False
        assert window.result.status == inert.Status.NOT_MEASURED

    def test_without_the_receiver_the_cli_exits_not_measured(self, live_app, hook_absent, capsys):
        code = inert_routes.main(SMALL_APP, app=_synthetic_app(), packages=PACKAGES)

        assert code == inert.EXIT_NOT_MEASURED == 3
        assert "NOT MEASURED" in capsys.readouterr().out


# --- the route model ---------------------------------------------------------------------


class TestRouteModel:
    def test_every_get_route_that_enqueues_today_is_detected(self, live_findings):
        flagged = {key: f for f in live_findings if f.witness for key in f.keys}

        missing = sorted(set(GET_ROUTES_THAT_ENQUEUE) - set(flagged))

        assert missing == []
        # One witness per route: the detail route now reaches the analytics queue first, so
        # the lazy redaction chain is pinned on a route that has only that one.
        assert (
            "redaction_detect_task.delay()"
            in flagged["GET /api/files/{file_uuid}/segments"].witness
        )
        assert "analyze_transcript_task" in flagged["GET /api/files/{file_uuid}"].witness
        assert "send_task()" in flagged["GET /api/system/stats"].witness

    def test_the_real_app_enqueues_on_exactly_the_known_get_routes(self, live_findings):
        flagged = {key for f in live_findings if f.safe and f.witness for key in f.keys}

        assert sorted(flagged - set(GET_ROUTES_THAT_ENQUEUE)) == [], (
            "a new safe-method route reaches a Celery publish; a demo visit would start "
            "background work. Make it inert, or add it to GET_ROUTES_THAT_ENQUEUE with why."
        )
        assert sorted(set(GET_ROUTES_THAT_ENQUEUE) - flagged) == []

    @pytest.mark.parametrize("key", ROUTES_THAT_MUST_STAY_CLEAN)
    def test_a_route_that_enqueues_nothing_stays_clean(self, live_findings, key):
        matches = [f for f in live_findings if key in f.keys]

        assert len(matches) == 1
        assert matches[0].witness is None

    def test_the_live_websocket_is_modelled_as_one(self, live_findings):
        sockets = [f for f in live_findings if f.kind == "websocket"]

        assert [f.path for f in sockets] == ["/api/ws"]
        assert sockets[0].methods == (inert.WEBSOCKET,)
        assert sockets[0].bypasses_http_middleware is True

    def test_a_websocket_that_enqueues_is_flagged(self):
        findings = inert_routes.model_routes(
            list(_synthetic_app(enqueue_on_ws=True).routes), _model()
        )
        by_path = {f.path: f for f in findings}

        assert by_path["/socket"].kind == "websocket"
        assert by_path["/socket"].safe is True
        assert "_noisy_socket -> " in str(by_path["/socket"].witness)
        assert by_path["/read"].witness is None

    def test_a_get_that_enqueues_is_flagged_and_a_post_is_not_a_visit(self):
        findings = inert_routes.model_routes(
            list(_synthetic_app(enqueue_on_get=True).routes), _model()
        )
        by_path = {f.path: f for f in findings}

        assert by_path["/read"].witness is not None
        assert by_path["/write"].witness is not None
        assert by_path["/write"].safe is False

    def test_in_process_background_work_on_a_get_is_flagged(self):
        from fastapi import BackgroundTasks
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/later")
        def later(background_tasks: BackgroundTasks):
            background_tasks.add_task(_helper_that_does_not, 1)
            return {}

        (finding,) = inert_routes.model_routes(list(app.routes)[-1:], _model())

        assert "background_tasks.add_task()" in str(finding.witness)

    def test_a_route_type_the_model_cannot_walk_fails_closed(self):
        from starlette.routing import Mount

        async def opaque_asgi(scope, receive, send):
            return None

        findings = inert_routes.model_routes([Mount("/static", app=opaque_asgi)], _model())

        assert [f.kind for f in findings] == ["unmodelled"]
        assert findings[0].safe is True
        assert findings[0].witness is not None

    def test_a_dependency_that_enqueues_is_followed(self):
        from fastapi import Depends
        from fastapi import FastAPI

        app = FastAPI()

        def noisy_dependency():
            _helper_that_enqueues(None)

        @app.get("/quiet-body")
        def quiet_body(_=Depends(noisy_dependency)):
            return {}

        by_path = {f.path: f for f in inert_routes.model_routes(list(app.routes), _model())}

        assert "noisy_dependency" in str(by_path["/quiet-body"].witness)

    def test_middleware_that_enqueues_is_reported_with_its_scope(self):
        from starlette.middleware import Middleware

        found = inert_routes.model_middleware([Middleware(EnqueuingHttpMiddleware)], _model())

        assert [(scope, "task.delay()" in witness) for _name, scope, witness in found] == [
            ("http", True)
        ]


# --- the beat plane ----------------------------------------------------------------------


class TestBeatClassification:
    def test_an_unclassified_entry_is_reported(self):
        schedule = {**BEAT_SCHEDULE, "new-sweep": {"task": "x", "schedule": 60.0}}

        assert inert.unclassified_beat_entries(schedule, BEAT_ENTRY_EFFECT) == ["new-sweep"]

    def test_a_classification_for_a_removed_entry_is_reported(self):
        effects = {**BEAT_ENTRY_EFFECT, "gone": BeatEffect.MUTATING}

        assert inert.unclassified_beat_entries(BEAT_SCHEDULE, effects) == [
            "gone (classified, not scheduled)"
        ]


# --- the verdict -------------------------------------------------------------------------


class TestVerdict:
    def test_a_quiet_app_is_inert(self, live_app, capsys, monkeypatch):
        monkeypatch.setattr(inert_routes, "active_beat_entries", list)
        assert (
            inert_routes.main(SMALL_APP, app=_synthetic_app(), packages=PACKAGES)
            == inert.EXIT_INERT
        )
        assert "assert-inert: INERT" in capsys.readouterr().out

    def test_an_app_that_enqueues_on_a_visit_is_not_inert(self, live_app, capsys):
        code = inert_routes.main(
            ["--json", *SMALL_APP], app=_synthetic_app(enqueue_on_get=True), packages=PACKAGES
        )

        assert code == inert.EXIT_NOT_INERT
        assert '"status": "NOT INERT"' in capsys.readouterr().out

    def test_the_real_app_is_not_inert_today(self, live_app):
        report = inert_routes.check(live_app)

        assert report.hook_measured is True
        assert report.status == inert.Status.NOT_INERT
        assert report.publishing_middleware == []
        assert report.unclassified_beat == []
        assert report.unresolved_imports == []

    def test_the_script_entry_checks_the_real_app(self, live_app, capsys):
        from app.scripts.assert_inert import run

        assert run(["--json"]) == inert.EXIT_NOT_INERT
        assert '"path": "/api/system/stats"' in capsys.readouterr().out


class TestMeasurementFloors:
    def test_an_empty_route_table_is_not_measured_not_inert(self, live_app, capsys):
        from fastapi import FastAPI

        code = inert_routes.main([], app=FastAPI(), packages=PACKAGES)

        assert code == inert.EXIT_NOT_MEASURED
        assert "routes modelled, floor is" in capsys.readouterr().out

    def test_the_floor_is_below_the_real_app_so_the_demo_gate_can_pass_it(self, live_app):
        report = inert_routes.check(live_app)

        assert report.routes_modelled >= inert.MIN_ROUTES_MODELLED
        assert report.not_measured_reasons == []

    def test_an_unresolved_import_is_not_measured(self, live_app):
        clean = inert_routes.check(_synthetic_app(), PACKAGES, min_routes=3)
        clean.demo_publish_refused = True
        assert clean.status == inert.Status.INERT

        gap = inert_routes.check(_synthetic_app(), PACKAGES, min_routes=3)
        gap.demo_publish_refused = True
        gap.unresolved_imports = ["app.missing"]

        assert gap.status == inert.Status.NOT_MEASURED
        assert "app.missing" in gap.not_measured_reasons[0]


class TestBeatEffectsAndDemoRefusal:
    def test_mutating_and_ingest_entries_make_a_live_deployment_not_inert(self, live_app):
        report = inert_routes.check(_synthetic_app(), PACKAGES, min_routes=3)

        assert report.demo_publish_refused is False
        assert report.active_beat, "the real beat table has MUTATING entries"
        assert report.status == inert.Status.NOT_INERT

    def test_a_proven_demo_publish_refusal_clears_routes_and_beat(self, live_app):
        live = inert_routes.check(_synthetic_app(enqueue_on_get=True), PACKAGES, min_routes=3)
        demo = inert_routes.check(_synthetic_app(enqueue_on_get=True), PACKAGES, min_routes=3)
        demo.demo_publish_refused = True

        assert live.status == inert.Status.NOT_INERT
        assert demo.status == inert.Status.INERT

    def test_an_unclassified_beat_entry_is_not_inert_even_under_the_refusal(self, live_app):
        report = inert_routes.check(_synthetic_app(), PACKAGES, min_routes=3)
        report.demo_publish_refused = True
        report.unclassified_beat = ["new-sweep"]

        assert report.status == inert.Status.NOT_INERT

    def test_the_probe_is_true_only_when_demo_mode_refuses(self, live_app, monkeypatch):
        from app.core.celery import celery_app
        from app.core.config import settings

        # conftest patches send_task on the instance; the probe must meet the real one.
        monkeypatch.delitem(vars(celery_app), "send_task", raising=False)
        monkeypatch.setattr(settings, "DEMO_MODE", False)
        assert inert.probe_demo_publish_refusal() is False

        monkeypatch.setattr(settings, "DEMO_MODE", True)
        assert inert.probe_demo_publish_refusal() is True
