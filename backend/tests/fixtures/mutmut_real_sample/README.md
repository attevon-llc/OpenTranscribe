A REAL, trimmed sample of what mutmut 3.8.1 writes, used by `tests/unit/test_mutation_survivor_report.py`.

- `generated.txt` (laid out as `mutants/app/auth/lockout_store.py`) is two functions copied verbatim from a real run's
  generated file (`x__get_redis_client__mutmut_orig` and `..._13`); `generated.spans.json` (the `.spans` file) is the `{"version": 1,
  "spans": {name: [first, last]}}` JSON mutmut writes (`mutmut/mutation/data.py`), with the line
  numbers renumbered to the trimmed file.
- `source.txt` (as `app/auth/lockout_store.py`) is the real source those mutants were generated from.
- `lockout_store.log` is the real summary-block line format of `scripts/run-mutation-tests.sh`.
