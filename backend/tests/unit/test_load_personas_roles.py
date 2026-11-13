"""The role personas of the load test (``scripts/perf/load/personas_roles.py``) and the phrasebook.

Run against the same Locust stand-ins and recording client as ``test_load_scenario_routes``:
every request each persona would make is recorded, so what a researcher searches for, what the
curator touches and what the canary asks are asserted on the wire, not on the source.
"""

from __future__ import annotations

import random

import pytest

from tests.fixtures import locust_stub as stub

ROLES = ("Researcher", "SpeakerCurator", "TeamLead", "StatusPoller", "WatcherBot")


def _book_for(load, seed):
    random.seed(seed)
    return load["phrasebook"].Phrasebook()


@pytest.fixture(scope="module")
def load():
    mp = pytest.MonkeyPatch()
    try:
        yield stub.load_modules(mp)
    finally:
        mp.undo()


@pytest.fixture(scope="module")
def run(load):
    return stub.run_personas(load["locustfile"], personas=ROLES + ("LoginHerd", "HotFileReader"))


def _calls(run, persona, **match):
    return [
        c
        for c in run.client.calls
        if c.persona == persona and all(getattr(c, k) == v for k, v in match.items())
    ]


# --- phrasebook ----------------------------------------------------------------------------


def test_the_phrasebook_asks_only_about_words_it_has_seen(load):
    book = _book_for(load, 1)
    book.learn_texts(["The quarterly budget review moved to Thursday.", "Hiring plan approved."])
    book.learn_names(["Dana Whitfield"])
    seen = {"quarterly", "budget", "review", "moved", "thursday", "hiring", "plan", "approved"}

    for _ in range(200):
        term = book.term()
        assert term == "Dana Whitfield" or set(term.split()) <= seen, term
        assert term[:1] != ""


def test_a_question_is_built_around_a_seen_phrase(load):
    book = _book_for(load, 2)
    book.learn_texts(["Migration timeline slipped again."])

    questions = {book.question() for _ in range(100)}

    assert all(q.endswith(("?", ".")) for q in questions)
    assert any("migration" in q or "timeline" in q or "slipped" in q for q in questions)


def test_an_empty_phrasebook_falls_back_instead_of_asking_nothing(load):
    book = _book_for(load, 3)

    assert book.empty
    assert book.term() in load["phrasebook"].FALLBACK
    assert 2 <= len(book.prefix()) <= 5


def test_stop_words_and_tiny_tokens_are_not_phrases(load):
    assert load["phrasebook"].content_words("It was the end of an era, and we are in it") == [
        "end",
        "era",
    ]


def test_the_same_seed_gives_the_same_questions(load):
    def ask(seed):
        random.seed(seed)
        book = load["phrasebook"].Phrasebook()
        book.learn_texts(["alpha beta gamma delta"])
        return [book.question() for _ in range(10)]

    assert ask(7) == ask(7)
    assert ask(7) != ask(8)


# --- the personas on the wire --------------------------------------------------------------


def test_the_researcher_searches_in_both_modes_with_terms_from_the_data(run):
    searches = _calls(run, "Researcher", url="/api/search")
    modes = {c.params["search_mode"] for c in searches}
    allowed = {"loadtest-mytag-1", "meeting", "budget", "schedule"}  # the stub's tag + fallback

    assert modes == {"keyword", "hybrid"}
    assert {c.params["q"] for c in searches} <= allowed
    assert not any(c.params["q"].startswith("loadtest ") for c in searches)  # no random numbers
    assert {c.name for c in searches} == {"search keyword", "search hybrid"}


def test_the_researcher_chats_over_a_collection_and_asks_a_phrasebook_question(run):
    created = [
        c
        for c in _calls(run, "Researcher")
        if c.url == "/api/chat/conversations" and c.method == "POST"
    ]
    sent = [
        c for c in _calls(run, "Researcher") if c.url.endswith("/messages") and c.method == "POST"
    ]

    assert any(c.body["scope"].get("collection_uuids") == ["col-1"] for c in created)
    assert sent and all(c.body["content"] != "What did they decide about the budget?" for c in sent)


def test_only_the_researchers_inside_the_chat_budget_chat(load):
    locustfile = load["locustfile"]
    researcher = locustfile.USER_CLASSES["Researcher"]
    researcher.chat_budget = 0
    try:
        none = stub.run_personas(locustfile, personas=("Researcher",), rounds=10)
    finally:
        researcher.chat_budget = 3

    assert not [c for c in none.client.calls if c.url.startswith("/api/chat/")]
    assert [c for c in none.client.calls if c.url == "/api/search"]


def test_the_curator_reviews_the_inbox_and_merges_speakers(run):
    urls = {c.url for c in _calls(run, "SpeakerCurator")}
    methods = {(c.method, c.url) for c in _calls(run, "SpeakerCurator")}

    assert "/api/speaker-clusters/unverified/inbox" in urls
    assert "/api/speaker-profiles/speakers/spk-1/suggestions" in urls
    assert any(m == "POST" and "/merge/" in u for m, u in methods)
    assert any(m == "PUT" and u == "/api/speakers/spk-new" for m, u in methods)


def test_the_team_lead_reads_summaries_shares_and_exports(run):
    urls = {c.url for c in _calls(run, "TeamLead")}

    assert "/api/collections/col-1/shares" in urls
    assert "/api/collections/shared-with-me" in urls
    assert any(u.endswith("/summary/export") for u in urls)
    assert any(u.endswith("/export") for u in urls)


def test_the_poller_polls_status_and_never_writes(run):
    calls = _calls(run, "StatusPoller")

    assert {c.url for c in calls} >= {"/api/tasks/progress/active", "/api/my-files/status"}
    assert all(c.method == "GET" or c.url == "/api/auth/login" for c in calls)


def test_the_watcher_holds_the_socket_like_the_listener(load):
    assert issubclass(load["personas_roles"].WatcherBot, load["ws_listener"].WebSocketListener)

    stub.run_personas(load["locustfile"], personas=("WatcherBot",), rounds=2)

    assert stub.StubWebsocket.sockets and stub.StubWebsocket.sockets[0].url.endswith("/api/ws")


def test_the_herd_signs_in_over_and_over(run):
    logins = _calls(run, "LoginHerd", url="/api/auth/login")

    assert len(logins) > 40  # on_start plus one per round of 40


def test_the_hot_file_reader_is_the_heavy_account_on_one_file(run, load):
    reads = [c for c in _calls(run, "HotFileReader") if c.name.endswith("hot")]
    files = {c.url for c in reads if "/files/" in c.url and "/comments" not in c.url}
    logins = _calls(run, "HotFileReader", url="/api/auth/login")

    assert {u.split("/")[3] for u in files} == {"file-a"}
    assert logins[0].body is None  # form-encoded login, identity chosen by the persona
    assert (
        load["personas_roles"].HotFileReader.identity_index(
            load["personas_roles"].HotFileReader.__new__(load["personas_roles"].HotFileReader)
        )
        == load["scenario"].HEAVY_INDEX
    )


def test_the_hot_file_write_cleans_up(run):
    created = _calls(run, "HotFileReader", method="POST")
    deleted = _calls(run, "HotFileReader", method="DELETE")
    assert any("/comments" in c.url for c in created)
    assert len(deleted) == len([c for c in created if "/comments" in c.url])


# --- the weights ---------------------------------------------------------------------------


def test_the_realistic_mix_is_dominated_by_researchers_and_background_tabs(load):
    classes = load["locustfile"].USER_CLASSES
    weights = {name: classes[name].weight for name in ROLES}
    total = sum(weights.values())

    assert weights == {
        "Researcher": 40,
        "SpeakerCurator": 8,
        "TeamLead": 12,
        "StatusPoller": 12,
        "WatcherBot": 18,
    }
    assert weights["Researcher"] / total > 0.4
    assert (weights["StatusPoller"] + weights["WatcherBot"]) / total > 0.25
    assert classes["Researcher"].fixed_count == 0


# --- the cross-tenant canary ---------------------------------------------------------------


def test_the_canary_asks_for_a_file_and_expects_to_be_refused(run):
    canaries = [c for c in run.client.calls if "canary" in c.name]

    assert {c.persona for c in canaries} == {"SpeakerCurator", "TeamLead"}  # the first user to
    assert all(c.url.startswith("/api/files/file-") for c in canaries)  # start has no stranger yet


def test_the_researcher_probes_a_file_another_identity_listed(load):
    owners = load["personas_base"].FILES_BY_OWNER
    owners["someone-else@loadtest.invalid"] = ["file-of-a-stranger"]
    try:
        researcher = stub.run_personas(load["locustfile"], personas=("Researcher",), rounds=40)
    finally:
        owners.pop("someone-else@loadtest.invalid")

    probes = [c for c in researcher.client.calls if "canary" in c.name]
    assert probes and {c.url for c in probes} <= {
        "/api/files/file-of-a-stranger",
        "/api/files/file-a",
        "/api/files/file-b",
        "/api/files/file-c",
    }
    assert any(c.url == "/api/files/file-of-a-stranger" for c in probes)


def test_a_server_that_answers_the_canary_with_200_makes_it_fail(load):
    # The stub raises on any failed call: a 200 to the canary is a failure, which is what lands
    # in the "cross-tenant canary" row that summarize.py holds to zero.
    with pytest.raises(AssertionError, match="HTTP 200"):
        stub.run_personas(
            load["locustfile"],
            personas=("TeamLead",),
            rounds=5,
            statuses={r"/api/files/file-[abc]": 200},
        )


def test_the_stub_merges_base_class_tasks_as_locust_does(load):
    # Locust 2.47 task.py: new_tasks += base.tasks. Overriding a task must not hide the base's.
    researcher = load["locustfile"].USER_CLASSES["Researcher"]
    chat_user = load["personas_write"].ChatUser

    assert set(chat_user.tasks) <= set(researcher.tasks)


def test_a_researcher_outside_the_chat_budget_never_chats_even_with_inherited_tasks(load):
    locustfile = load["locustfile"]
    researcher = locustfile.USER_CLASSES["Researcher"]
    researcher.chat_budget = 0
    try:
        run = stub.run_personas(locustfile, personas=("Researcher",), rounds=10)
    finally:
        researcher.chat_budget = 3

    assert not [c for c in run.client.calls if c.url.startswith("/api/chat/")]
    assert not [c for c in run.client.calls if c.method == "POST" and c.url.startswith("/api/llm")]


def test_the_summary_persona_asks_only_for_formats_the_endpoint_supports(load):
    from app.services.summary_export_service import VALID_SUMMARY_EXPORT_FORMATS

    assert set(load["personas_roles"].SUMMARY_FORMATS) <= set(VALID_SUMMARY_EXPORT_FORMATS)
