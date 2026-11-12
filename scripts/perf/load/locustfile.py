"""Concurrent-user load test for OpenTranscribe (local ``--fresh`` stacks only).

    scripts/perf/load/run.sh --fresh perf1337 --profile ramp      # the runbook's way in
    locust -f scripts/perf/load/locustfile.py --fresh perf1337 --load-profile smoke --headless

Simulated people sign in as the scale seed's identities (``perf-N@loadtest.invalid``) with the
SPA's own cookie and CSRF flow and act as one of the personas (``personas_read``,
``personas_write``, ``personas_roles``, ``ws_listener``): viewers, the heavy owner, editors, chat users, the
administrator, the organization administrator and open browser tabs holding the live socket. The
``people`` profiles use the role personas of ``personas_roles`` instead (researcher, speaker
curator, team lead, status poller, watcher tab) in a realistic mix.
``--load-profile`` picks the load shape (``profiles.py``); ``--exclude-tags write`` makes a run
read-only. The runbook is ``scripts/perf/load/README.md``.

Every HTTP request goes through ``routes.ROUTES``, which
``backend/tests/unit/test_load_scenario_routes.py`` checks against ``backend/openapi.json``.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import profiles
import scenario
from locust import LoadTestShape, events
from personas_read import Administrator, HeavyOwner, OrgAdmin, Viewer
from personas_roles import (
    HotFileReader,
    LoginHerd,
    Researcher,
    SpeakerCurator,
    StatusPoller,
    TeamLead,
    WatcherBot,
)
from personas_write import ChatUser, Editor, PipelineUploader
from ws_listener import WebSocketListener

logger = logging.getLogger('ot-load')

USER_CLASSES: dict[str, Any] = {
    cls.__name__: cls
    for cls in (
        Viewer,
        HeavyOwner,
        Editor,
        ChatUser,
        Administrator,
        OrgAdmin,
        WebSocketListener,
        PipelineUploader,
        Researcher,
        SpeakerCurator,
        TeamLead,
        StatusPoller,
        WatcherBot,
        LoginHerd,
        HotFileReader,
    )
}


@events.init_command_line_parser.add_listener
def _add_arguments(parser: Any) -> None:
    parser.add_argument('--fresh', default='', help='the ./opentr.sh --fresh deployment to load')
    parser.add_argument(
        '--load-profile',
        default='smoke',
        choices=sorted(profiles.PROFILES),
        help='load shape: '
        + '; '.join(f'{p.name}: {p.summary}' for p in profiles.PROFILES.values()),
    )
    parser.add_argument(
        '--target-users', type=int, default=None, help='users for steady/spike/soak/pipeline'
    )
    parser.add_argument('--ramp-rate', type=float, default=None, help='users started per second')
    parser.add_argument('--hold', type=float, default=None, help='seconds to hold each stage')
    parser.add_argument('--steps', default=None, help='ramp steps, e.g. 100,250,500,1000')
    parser.add_argument(
        '--chat-users',
        type=int,
        default=None,
        help='chat users (fixed count; chat turns are rate limited per client address)',
    )
    parser.add_argument(
        '--mock-llm-url', default='http://mock-llm:5199/v1', help='as the backend reaches it'
    )
    parser.add_argument(
        '--seeded-users', type=int, default=1000, help='users the seed created (its --users)'
    )
    parser.add_argument(
        '--request-timeout', type=float, default=60.0, help='seconds before a call counts as failed'
    )
    parser.add_argument(
        '--pipeline-timeout', type=float, default=900.0, help='seconds an upload may take to finish'
    )
    parser.add_argument(
        '--pipeline-poll', type=float, default=5.0, help='seconds between status polls'
    )


@events.init.add_listener
def _guard(environment: Any, **_kwargs: Any) -> None:
    options = environment.parsed_options
    if options is None:  # imported as a library
        return
    try:
        target = scenario.resolve_target(options.fresh, options.host or None)
        profile = profiles.get(options.load_profile)
        scenario.pool_bounds('viewer', options.seeded_users)
    except (scenario.NotIsolatedError, KeyError, ValueError) as exc:
        logger.error('%s', exc)
        sys.exit(2)
    environment.host = target.base_url
    ChatUser.fixed_count = (
        options.chat_users if options.chat_users is not None else profile.chat_users
    )
    Researcher.chat_budget = ChatUser.fixed_count
    logger.info('Loading --fresh %s at %s, profile %s', target.fresh, target.base_url, profile.name)


class ProfileShape(LoadTestShape):
    """Hands Locust the user count, spawn rate and personas of the ``--load-profile`` in force."""

    def tick(self) -> tuple[int, float, list[Any]] | None:
        options = self.runner.environment.parsed_options if self.runner else None
        profile = profiles.get(options.load_profile if options else 'smoke')
        step = profile.tick(self.get_run_time(), profiles.Params.from_options(options))
        if step is None:
            return None
        users, rate, personas = step
        return users, rate, [USER_CLASSES[name] for name in personas]
