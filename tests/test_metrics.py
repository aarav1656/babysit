import json
import time
import unittest

from babysit.card import bar, fmt_duration, fmt_num, render_card, sparkline
from babysit.metrics import MAX_KEYS, MAX_PAIRS, MAX_POINTS, MetricStore, primary_key


def fed(*lines, start=0.0, step=0.5):
    store = MetricStore(t0=start)
    for index, line in enumerate(lines):
        store.feed(line, start + index * step)
    return store


class ParseTests(unittest.TestCase):
    def test_key_value_and_key_colon_value(self):
        store = fed("step 4120/10000 epoch 3 loss=1.83 lr=3e-4 grad_norm: 0.91 val/loss = 2.5 acc=91.2%")
        self.assertEqual(sorted(store.series), ["acc", "grad_norm", "loss", "lr", "val/loss"])
        self.assertEqual(store.values("loss"), [1.83])
        self.assertEqual(store.values("lr"), [3e-4])
        self.assertEqual(store.values("val/loss"), [2.5])
        self.assertEqual(store.values("acc"), [91.2])
        self.assertEqual((store.step, store.total), (4120, 10000))
        self.assertEqual(store.epoch, (3, None))

    def test_signs_exponents_and_case(self):
        store = fed("Loss=-0.5 LR: 1E-3 delta=+2")
        self.assertEqual(store.values("loss"), [-0.5])
        self.assertEqual(store.values("lr"), [1e-3])
        self.assertEqual(store.values("delta"), [2.0])

    def test_non_finite_values_never_enter_a_series(self):
        store = fed("loss=nan lr=inf acc=-inf val_loss: Infinity", "step 2 loss=1.0")
        self.assertEqual(sorted(store.series), ["loss"])
        self.assertEqual(store.values("loss"), [1.0])

    def test_config_echoes_are_not_metrics(self):
        store = fed("max_grad_norm: inf", "--clip=3 --lr=0.1", "Namespace(max_steps=100, min_lr=1e-6, timeout=5)",
                    "default_batch=32 init_scale=2 grad_clip: 1.0")
        self.assertEqual(store.series, {})

    def test_step_total_formats(self):
        for line, step, total in (("step 4120/10000", 4120, 10000), ("Step: 8 of 20", 8, 20), ("iter=12/50", 12, 50),
                                  ("global_step 7", 7, None), ("iteration #3", 3, None), ("steps 5/9]", 5, 9)):
            with self.subTest(line=line):
                store = fed(line)
                self.assertEqual((store.step, store.total), (step, total))

    def test_total_is_remembered_for_later_lines(self):
        store = fed("step 10/200", "step 50", "loss=1")
        self.assertEqual((store.step, store.total, store.frac), (50, 200, 0.25))

    def test_step_is_the_x_of_later_samples_and_the_line_count_without_one(self):
        store = fed("step 7 loss=1.0", "loss=0.9")
        self.assertEqual([x for x, _t, _v in store.series["loss"]], [7, 7])
        plain = fed("loss=1.0", "loss=0.9")
        self.assertEqual([x for x, _t, _v in plain.series["loss"]], [1, 2])

    def test_tqdm_line(self):
        store = fed(" 41%|████      | 4120/10000 [12:30<17:40,  1.92it/s]")
        self.assertEqual((store.step, store.total), (4120, 10000))
        self.assertAlmostEqual(store.frac, 0.412)
        self.assertEqual(store.rate(), 1.92)

    def test_percent_alone_counts_only_as_progress_in_bar_or_labelled_form(self):
        self.assertAlmostEqual(fed("downloading 41%|███   | 41/100")._progress[-1][1], 0.41)
        self.assertAlmostEqual(fed("progress: 63.5%").frac, 0.635)
        self.assertIsNone(fed("val acc 91.2% on 5000 samples").frac)

    def test_rate_hint_forms(self):
        self.assertEqual(fed("1.9 it/s").rate(), 1.9)
        self.assertEqual(fed("loss=1 it/s=2.5").rate(), 2.5)
        self.assertEqual(fed("0.5s/it").rate(), 2.0)
        self.assertEqual(fed("12 steps/s").rate(), 12.0)
        self.assertIsNone(fed("loss=1").rate())

    def test_fraction_never_exceeds_one(self):
        self.assertEqual(fed("step 120/100").frac, 1.0)


class BoundTests(unittest.TestCase):
    def test_each_series_is_a_ring_of_the_newest_points(self):
        store = MetricStore()
        for i in range(MAX_POINTS * 3):
            store.feed(f"step {i} loss={i}", i)
        self.assertEqual(len(store.series["loss"]), MAX_POINTS)
        self.assertEqual(store.values("loss")[0], MAX_POINTS * 2)
        self.assertEqual(store.values("loss")[-1], MAX_POINTS * 3 - 1)

    def test_distinct_names_are_capped(self):
        store = fed(*(f"metric{i}=1" for i in range(MAX_KEYS * 4)))
        self.assertEqual(len(store.series), MAX_KEYS)

    def test_pairs_per_line_are_capped(self):
        store = fed(" ".join("a=1" for _ in range(MAX_PAIRS * 5)))
        self.assertEqual(len(store.series["a"]), MAX_PAIRS)

    def test_events_fold_repeats_and_do_not_evict_earlier_kinds(self):
        store = fed("step 1")
        store.mark("alert", "validation failed", 1.0)
        for i in range(500):
            store.mark("nan", "loss=nan", 2.0 + i * 0.5)
        kinds = [event[2] for event in store.events]
        self.assertEqual(kinds, ["alert", "nan"])
        self.assertEqual(store.events[1][4], 500)
        store.mark("nan", "loss=nan again", 2.0 + 500 * 0.5 + 60)
        self.assertEqual([event[2] for event in store.events], ["alert", "nan", "nan"])

    def test_event_sits_at_the_step_it_happened(self):
        store = fed("step 81 loss=nan")
        store.mark("nan", "step 81 loss=nan", 5.0)
        self.assertEqual(store.snapshot()["events"][0]["x"], 81)


class AdversarialLineTests(unittest.TestCase):
    """Lines run up to 1 MiB. A quadratic pattern takes minutes on these; linear takes milliseconds."""

    N = 1 << 20

    def check(self, line):
        store = MetricStore(t0=0)
        started = time.perf_counter()
        store.feed(line, 1.0)
        self.assertLess(time.perf_counter() - started, 2.0, line[:40])

    def test_digit_runs(self):
        for line in ("1" * self.N, "loss=" + "1" * self.N, "loss=" + "1" * self.N + "x", "step " + "9" * self.N,
                     "step 1/" + "1" * self.N, "1" * self.N + "%|", "1" * self.N + "/" + "1" * self.N + " [",
                     "loss=1." + "1" * self.N + "e", "9" * self.N + " it/s"):
            with self.subTest(line=line[:30]):
                self.check(line)

    def test_name_and_gap_runs(self):
        for line in ("a" * self.N, "a" * self.N + "=1", "a-" * (self.N // 2), "a/" * (self.N // 2) + "=1",
                     "loss" + " " * self.N + "=1", "loss=" + " " * self.N + "1", "loss:" + "\t" * self.N,
                     " " * self.N + "loss=1", "step" + " " * self.N + "1", "progress" + " " * self.N + "5%"):
            with self.subTest(line=line[:30]):
                self.check(line)

    def test_repeated_fragments(self):
        for line in ("a=1 " * (self.N // 4), "a:" * (self.N // 2), "%" * self.N, "step 1 " * (self.N // 7),
                     "1/2 [" * (self.N // 5), "loss=nan " * (self.N // 9), "[" * self.N, "=" * self.N, "e" * self.N + "-5"):
            with self.subTest(line=line[:30]):
                self.check(line)

    def test_hostile_line_leaves_a_consistent_store(self):
        store = MetricStore(t0=0)
        store.feed("a=1 " * (self.N // 4), 1.0)
        self.assertEqual(len(store.series["a"]), MAX_PAIRS)
        json.dumps(store.snapshot(), allow_nan=False)


class RateAndEtaTests(unittest.TestCase):
    def test_measured_rate_and_eta(self):
        # 2 steps per second over a 100 step job: at step 40 (t=20s) there are 30 s left
        store = fed(*(f"step {i}/100 loss=1" for i in range(1, 41)), step=0.5)
        self.assertAlmostEqual(store.fraction_rate(), 0.02, places=3)
        self.assertAlmostEqual(store.eta(), 30.0, delta=1.5)
        self.assertAlmostEqual(store.rate(), 2.0, delta=0.1)

    def test_a_batch_read_at_one_instant_falls_back_to_the_printed_rate_not_a_division_by_zero(self):
        store = MetricStore(t0=0)
        for i in range(1, 51):
            store.feed(f"step {i}/100 loss=1 it/s=4", 7.0)
        self.assertEqual(store.eta(), (1 - 0.5) / (4 / 100))

    def test_no_rate_no_eta(self):
        self.assertIsNone(fed("step 10/100", "step 11/100", step=0.0).eta())
        self.assertIsNone(fed("step 10", "step 20", step=5.0).eta())  # no total
        self.assertIsNone(fed("loss=1").eta())

    def test_stuck_job_has_no_eta(self):
        store = fed(*("step 10/100" for _ in range(30)), step=1.0)
        self.assertIsNone(store.eta())

    def test_eta_reaches_zero_at_the_end(self):
        store = fed(*(f"step {i}/10" for i in range(1, 11)), step=1.0)
        self.assertEqual(store.eta(), 0.0)

    def test_percent_only_job_gets_an_eta(self):
        store = fed(*(f"{i}%|" + "#" * i + "| x" for i in range(10, 31, 5)), step=10.0)
        self.assertAlmostEqual(store.eta(), (1 - 0.3) / (0.2 / 40), delta=1)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_json_safe_thinned_and_ends_on_the_newest_point(self):
        store = MetricStore(t0=0)
        for i in range(600):
            store.feed(f"step {i}/600 loss={1 / (i + 1)} lr=0.1", i * 0.1)
        snap = store.snapshot()
        json.dumps(snap, allow_nan=False)
        self.assertEqual(snap["primary"], "loss")
        self.assertEqual(list(snap["series"])[0], "loss")
        points = snap["series"]["loss"]
        self.assertEqual(len(points), 240)
        self.assertEqual(points[0][0], 0)
        self.assertEqual(points[-1][0], 599)

    def test_series_count_is_capped_and_lead_metric_first(self):
        store = fed("step 1 " + " ".join(f"m{i}=1" for i in range(20)) + " val_loss=3 loss=2", "step 2 loss=1")
        snap = store.snapshot()
        self.assertEqual(len(snap["series"]), 8)
        self.assertEqual(next(iter(snap["series"])), "loss")

    def test_primary_key_prefers_training_loss(self):
        self.assertEqual(primary_key(["lr", "val_loss", "train_loss"]), "train_loss")
        self.assertEqual(primary_key(["lr", "val_loss"]), "val_loss")
        self.assertEqual(primary_key(["lr", "acc"]), "lr")
        self.assertEqual(primary_key(["acc", "loss"]), "loss")
        self.assertIsNone(primary_key([]))


class SparklineTests(unittest.TestCase):
    def test_falling_values_fall(self):
        line = sparkline([8, 7, 6, 5, 4, 3, 2, 1])
        self.assertEqual(line, "█▇▆▅▄▃▂▁")

    def test_rising_flat_empty_and_non_finite(self):
        self.assertEqual(sparkline([1, 2]), "▁█")
        self.assertEqual(sparkline([5, 5, 5]), "▄▄▄")
        self.assertEqual(sparkline([]), "")
        self.assertEqual(sparkline([float("nan"), float("inf")]), "")
        self.assertEqual(sparkline([1, float("nan"), 2]), "▁█")

    def test_long_series_is_averaged_down_to_the_width(self):
        line = sparkline(list(range(100, 0, -1)), width=10)
        self.assertEqual(len(line), 10)
        self.assertEqual(line[0], "█")
        self.assertEqual(line[-1], "▁")
        self.assertEqual(list(line), sorted(line, reverse=True))


class CardTests(unittest.TestCase):
    def test_bar_widths_and_resolution(self):
        self.assertEqual(bar(0, 8), "▕        ▏")
        self.assertEqual(bar(1, 8), "▕████████▏")
        self.assertEqual(bar(0.5, 8), "▕████    ▏")
        self.assertEqual(bar(0.5625, 8), "▕████▌   ▏")
        self.assertEqual(bar(7, 8), bar(1, 8))
        self.assertEqual(bar(-1, 8), bar(0, 8))
        for frac in (0.0, 0.01, 0.33, 0.41, 0.999, 1.0):
            self.assertEqual(len(bar(frac, 20)), 22)

    def test_number_and_duration_formats(self):
        self.assertEqual([fmt_num(v) for v in (1.8312, 3e-4, 0.1, 0, 123456, 0.0025, -0.5)],
                         ["1.83", "3e-4", "0.1", "0", "1.23e5", "0.0025", "-0.5"])
        self.assertEqual([fmt_duration(s) for s in (0, 59, 60, 725, 3600, 3900, 86400 * 3)],
                         ["0s", "59s", "1m", "12m", "1h00m", "1h05m", "3d0h"])

    def test_progress_card(self):
        store = MetricStore(t0=0)
        for i in range(1, 42):
            store.feed(f"step {i * 100}/10000 loss={3 - i * 0.04:.2f} lr=3e-4 it/s=1.9", i * 0.5)
        card = render_card("train.log", store, "ignored", 20.5, 41).split("\n")
        self.assertEqual(card[0], "train.log  step 4100/10000  41%")
        self.assertRegex(card[1], r"^▕█{8}[▏▎▍▌▋▊▉]? +▏ ETA \d+[ms]")
        self.assertRegex(card[2], r"^loss 1\.36 [▁-█]{10}  lr 3e-4  1\.9 it/s$")
        self.assertEqual(card[3], "Elapsed: 20s | Lines seen: 41")
        self.assertEqual(len(card), 4)

    def test_event_card_leads_with_the_marker_and_the_event_line(self):
        store = MetricStore(t0=0)
        for i in range(1, 6):
            store.feed(f"step {i}/100 loss=1.5", i)
        for kind, marker in (("nan", "✖"), ("oom", "✖"), ("traceback", "✖"), ("alert", "⚠"), ("stall", "⚠"),
                             ("process ended", "■"), ("watch stopped", "■")):
            card = render_card("train.log", store, "x", 5, 5, (kind, "loss=nan lr=0.1")).split("\n")
            self.assertEqual(card[0], f"{marker} train.log: {kind}: loss=nan lr=0.1")
            self.assertIn("step 5/100", card[1])
            self.assertIn("5%", card[1])
            self.assertTrue(card[2].startswith("loss 1.5"))

    def test_log_without_steps_keeps_the_plain_status_line(self):
        store = fed("starting up", "warming caches")
        self.assertEqual(render_card("job.log", store, "warming caches", 3, 2), "job.log: warming caches\nElapsed: 3s | Lines seen: 2")
        self.assertEqual(render_card("job.log", fed(), "", 1, 0), "job.log:\nElapsed: 1s | Lines seen: 0")
        self.assertEqual(render_card("job.log", fed(), "", 1, 0, ("stall", "")), "⚠ job.log: stall:\nElapsed: 1s | Lines seen: 0")

    def test_finished_job_says_done_and_unknown_eta_says_so(self):
        done = fed("step 10/10 loss=0.1")
        self.assertIn("done", render_card("j", done, "", 1, 1).split("\n")[1])
        unknown = fed("step 5/10 loss=0.1")
        self.assertTrue(render_card("j", unknown, "", 1, 1).split("\n")[1].endswith("ETA ?"))

    def test_extra_metric_is_the_first_known_of_the_preferred_list(self):
        store = fed("step 1/9 loss=1 acc=0.5 grad_norm=2", "step 2/9 loss=1 acc=0.6 grad_norm=2")
        self.assertIn("acc 0.6", render_card("j", store, "", 1, 2))
        self.assertNotIn("grad_norm", render_card("j", store, "", 1, 2))


if __name__ == "__main__":
    unittest.main()
