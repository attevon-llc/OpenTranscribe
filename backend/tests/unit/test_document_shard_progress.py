"""One coherent percentage across N OCR shards — the owner requirement, pinned.

*"OCR shards a big scan ~20 pages at a time — roll shard progress up to the parent task
so the modal shows one coherent percentage, not N mystery tasks."*

Three properties make that true and each has a way of being quietly false:

* **Monotonic and single-scaled.** Stage bands, not per-stage 0→100. A bar that resets
  three times is three bars.
* **Aggregated across PROCESSES.** Shards run in different Celery workers, so the counter
  has to be in Redis and the increments have to be atomic. A test that only ever
  increments from one process cannot tell an atomic counter from a racy one, so the
  concurrency case is exercised against a real Redis.
* **Terminal even when a shard fails.** A failed shard that does not count toward
  completion leaves the bar at 97 % forever, which is the most common way a progress bar
  lies.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

from app.services.documents.progress import STAGE_BANDS
from app.services.documents.progress import ShardLedger
from app.services.documents.progress import ShardRollup
from app.services.documents.progress import overall_progress
from app.services.documents.progress import plan_shards


class TestStageBands:
    def test_the_bands_tile_zero_to_one_hundred_without_gaps_or_overlaps(self):
        """A gap is a bar that jumps; an overlap is a bar that goes backwards."""
        bands = list(STAGE_BANDS.values())
        assert bands[0][0] == 0.0
        assert bands[-1][1] == 100.0
        for (_, end), (start, _) in zip(bands, bands[1:], strict=False):
            assert end == start, f"band boundary {end} != {start}"

    def test_ocr_owns_the_largest_band(self):
        """It dominates wall clock by an order of magnitude (~13 s/page against ~4 ms for
        a text layer). A bar weighted by stage COUNT would sit at 40 % for four minutes."""
        widths = {name: end - start for name, (start, end) in STAGE_BANDS.items()}
        assert widths["ocr"] == max(widths.values())
        assert widths["ocr"] > sum(w for n, w in widths.items() if n != "ocr")

    @pytest.mark.parametrize(
        ("fraction", "expected"),
        [(0.0, 15.0), (0.5, 50.0), (1.0, 85.0)],
    )
    def test_within_stage_progress_maps_into_the_stage_band(self, fraction, expected):
        assert overall_progress("ocr", fraction) == pytest.approx(expected)

    def test_progress_is_clamped_rather_than_allowed_past_its_band(self):
        assert overall_progress("ocr", 1.5) == pytest.approx(85.0)
        assert overall_progress("ocr", -1.0) == pytest.approx(15.0)

    def test_an_unknown_stage_raises_rather_than_silently_reporting_zero(self):
        """A typo'd stage mapping to 0 makes the bar jump backwards, which reads as a hang."""
        with pytest.raises(KeyError):
            overall_progress("ocrr", 0.5)

    def test_the_stages_are_ordered_so_progress_never_decreases(self):
        sequence = [
            overall_progress(stage, 1.0) for stage in ("download", "parse", "ocr", "chunk", "store")
        ]
        assert sequence == sorted(sequence)


class TestTheRollupArithmetic:
    def test_progress_is_measured_in_pages_not_shards(self):
        """Shards are not equal work — the last shard of a 47-page scan holds 7 pages.
        Counting shards makes the bar stall for a full shard and then jump."""
        rollup = ShardRollup(
            done_shards=2, total_shards=3, done_pages=40, total_pages=47, failed_shards=0
        )
        assert rollup.fraction == pytest.approx(40 / 47)
        assert rollup.fraction != pytest.approx(2 / 3)

    def test_it_falls_back_to_counting_shards_when_the_page_total_is_unknown(self):
        rollup = ShardRollup(
            done_shards=1, total_shards=4, done_pages=0, total_pages=0, failed_shards=0
        )
        assert rollup.fraction == pytest.approx(0.25)

    def test_an_empty_ledger_is_zero_rather_than_a_division_error(self):
        assert ShardRollup(0, 0, 0, 0, 0).fraction == 0.0

    def test_a_failed_shard_still_completes_the_document(self):
        """Otherwise one unreadable shard parks the parent task at 97 % forever."""
        rollup = ShardRollup(
            done_shards=2, total_shards=3, done_pages=40, total_pages=60, failed_shards=1
        )
        assert rollup.complete is True
        # The point of the claim: completion is reached with pages still outstanding.
        # Asserting only `complete` would pass just as well on a rollup that had
        # finished all 60 pages, which is not the case this test exists to cover.
        assert rollup.fraction == pytest.approx(40 / 60)
        assert rollup.done_shards + rollup.failed_shards == rollup.total_shards

    def test_an_incomplete_run_is_not_reported_complete(self):
        """The negative control: `complete` returning True unconditionally would satisfy
        the test above."""
        rollup = ShardRollup(
            done_shards=2, total_shards=3, done_pages=40, total_pages=60, failed_shards=0
        )
        assert not rollup.complete

    def test_the_description_names_the_shortfall_when_shards_failed(self):
        """Silent degradation must surface. A document short by two sections of OCR reads
        as a short document unless it is said."""
        clean = ShardRollup(3, 3, 60, 60, 0)
        degraded = ShardRollup(2, 3, 40, 60, 1)
        assert "could not be read" not in clean.describe()
        assert "1 section(s) could not be read" in degraded.describe()

    def test_the_overall_number_stays_inside_the_ocr_band(self):
        for done in range(0, 61, 10):
            rollup = ShardRollup(done // 20, 3, done, 60, 0)
            assert 15.0 <= rollup.overall <= 85.0


class TestShardPlanning:
    def test_a_five_hundred_page_scan_becomes_twenty_five_twenty_page_shards(self):
        """The plan's own worked example: 25 interleaved ~1-minute tasks instead of one
        25-minute queue-starver, under a global worker_prefetch_multiplier=1."""
        shards = plan_shards(500, 20)
        assert len(shards) == 25
        assert shards[0] == (1, 20)
        assert shards[-1] == (481, 500)

    def test_the_ranges_are_contiguous_and_cover_every_page_exactly_once(self):
        shards = plan_shards(47, 20)
        assert shards == [(1, 20), (21, 40), (41, 47)]
        covered = [page for first, last in shards for page in range(first, last + 1)]
        assert covered == list(range(1, 48))

    def test_a_document_shorter_than_one_shard_is_one_shard(self):
        assert plan_shards(3, 20) == [(1, 3)]

    @pytest.mark.parametrize(("pages", "size"), [(0, 20), (-1, 20), (10, 0)])
    def test_degenerate_inputs_produce_no_shards_rather_than_an_infinite_loop(self, pages, size):
        assert plan_shards(pages, size) == []


def _redis_usable() -> bool:
    """Can this process actually talk to Redis — not just open a socket to the port?

    A TCP probe is not enough: the stack's Redis requires ``REDIS_PASSWORD``, and an
    unauthenticated client connects fine and then fails on the first command with
    ``AuthenticationError``. That is the difference between a clean skip and eight
    confusing failures, and it is the same class of mistake the root conftest's comment
    records about probing one port and using another.
    """
    # Deliberately keyed on an EXPLICIT ``REDIS_PORT``, not on ``SKIP_REDIS``: the root
    # conftest hard-sets ``SKIP_REDIS=True`` for the whole suite (not ``setdefault``), so a
    # shell override cannot win, and the default ``localhost:6379`` is exactly the
    # unrelated-container hazard conftest's own Celery comment records. Requiring the port
    # to be named means these run only when someone pointed them at a Redis on purpose.
    if not os.environ.get("REDIS_PORT"):
        return False
    try:
        from app.core.redis import get_redis

        return bool(get_redis().ping())
    except Exception:  # noqa: BLE001 - any failure means "cannot run these"
        return False


@pytest.mark.skipif(
    not _redis_usable(),
    reason=(
        "Redis is not usable from this process. The ledger exists BECAUSE shards run in "
        "different processes, and an in-process fake cannot tell an atomic HINCRBY from a "
        "read-modify-write race — so these do not get a stand-in. Set REDIS_PORT (and "
        "REDIS_PASSWORD if the target needs one) to opt in: "
        "`REDIS_HOST=127.0.0.1 REDIS_PORT=<port> pytest "
        "tests/unit/test_document_shard_progress.py`. Verified this way against a real "
        "Redis 7. The 21 arithmetic tests above run everywhere and cover the band mapping, "
        "the page-weighted fraction, failed-shard completion and the shard plan."
    ),
)
class TestTheLedgerAgainstRealRedis:
    """The concurrency property, exercised rather than asserted about."""

    @pytest.fixture
    def ledger(self):
        led = ShardLedger(f"test-parse-{uuid.uuid4().hex[:12]}")
        yield led
        led.clear()

    def test_begin_declares_the_work_and_read_reports_nothing_done(self, ledger):
        ledger.begin(total_shards=5, total_pages=100)
        rollup = ledger.read()
        assert rollup.total_shards == 5
        assert rollup.total_pages == 100
        assert rollup.done_shards == 0
        assert not rollup.complete

    def test_recording_every_shard_reaches_complete_and_one_hundred_percent(self, ledger):
        ledger.begin(total_shards=3, total_pages=60)
        rollups = [ledger.record(pages_done=20) for _ in range(3)]

        assert [r.done_shards for r in rollups] == [1, 2, 3]
        assert rollups[-1].complete
        assert rollups[-1].fraction == pytest.approx(1.0)
        assert rollups[-1].overall == pytest.approx(85.0)

    def test_twenty_five_shards_reporting_concurrently_produce_one_correct_aggregate(self, ledger):
        """The test the in-process fake cannot do.

        A read-modify-write counter loses increments under this and reports fewer than 25
        done shards — which surfaces as a document stuck below 100 %.
        """
        ledger.begin(total_shards=25, total_pages=500)
        errors: list[BaseException] = []

        def report() -> None:
            try:
                ledger.record(pages_done=20)
            except BaseException as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=report) for _ in range(25)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, f"{len(errors)} shard reports raised: {errors[:3]}"
        final = ledger.read()
        assert final.done_shards == 25, "increments were lost — the counter is not atomic"
        assert final.done_pages == 500
        assert final.complete

    def test_a_failed_shard_is_counted_separately_but_still_completes_the_document(self, ledger):
        ledger.begin(total_shards=2, total_pages=40)
        ledger.record(pages_done=20)
        final = ledger.record(pages_done=0, failed=True)

        assert final.done_shards == 1
        assert final.failed_shards == 1
        assert final.complete
        assert "could not be read" in final.describe()

    def test_begin_overwrites_a_previous_plan_rather_than_merging_into_it(self, ledger):
        """A retried parse re-shards the document. Merged, the bar would report progress
        against the union of two different shard plans."""
        ledger.begin(total_shards=10, total_pages=200)
        ledger.record(pages_done=20)
        assert ledger.read().done_shards == 1

        ledger.begin(total_shards=3, total_pages=60)
        restarted = ledger.read()
        assert restarted.total_shards == 3
        assert restarted.done_shards == 0
        assert restarted.done_pages == 0

    def test_reading_an_expired_or_absent_ledger_is_zero_not_a_crash(self):
        """The TTL can outlive a very slow parse. Reporting no progress beats taking down
        the task that was about to finish."""
        absent = ShardLedger(f"never-created-{uuid.uuid4().hex[:12]}")
        rollup = absent.read()
        assert rollup == ShardRollup(0, 0, 0, 0, 0)
        assert rollup.overall == pytest.approx(15.0)

    def test_clear_removes_the_ledger(self, ledger):
        ledger.begin(total_shards=1, total_pages=10)
        ledger.record(pages_done=10)
        assert ledger.read().total_shards == 1

        ledger.clear()
        assert ledger.read().total_shards == 0
