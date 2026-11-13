A REAL, trimmed sample of what mutmut 3.8.1 writes, used by `tests/unit/test_mutation_survivor_report.py`.

- `generated.txt` (laid out as `mutants/app/auth/lockout_store.py`) is two functions copied verbatim from a real run's
  generated file (`x__get_redis_client__mutmut_orig` and `..._13`); `generated.spans.json` (the `.spans` file) is the `{"version": 1,
  "spans": {name: [first, last]}}` JSON mutmut writes (`mutmut/mutation/data.py`), with the line
  numbers renumbered to the trimmed file.
- `source.txt` (as `app/auth/lockout_store.py`) is the real source those mutants were generated from.
- `summary.txt` (copied to `<key>.log` by the test) is four lines of a real run's summary block
  (the 2026-11-13 `--parallel` run at f89d44d88): the BEGIN marker, `_12` killed,
  `_13` survived, and the END marker, whose counts are the untrimmed run's. Not named `*.log`: the
  repo's `.gitignore` drops `*.log`, so a `.log` fixture is never committed and the test only passed
  in the checkout that wrote it.
