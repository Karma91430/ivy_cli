# IVY Coding-Agent Test Protocol

A reproducible suite that compares IVY's coding agent against Claude Code on the dimensions that actually distinguish them in practice: tool selection, edit precision, multi-step planning, and error recovery.

## What "similar to Claude Code" means here

For each test, the rubric scores four observable behaviors. **Same prompt, fresh fixture dir, same hardware.**

| Rubric            | Pass criteria                                                                |
| ----------------- | ---------------------------------------------------------------------------- |
| **Correctness**   | Final filesystem state matches the expected outcome.                         |
| **Tool choice**   | Used the right tool (`str_replace_in_file` not full rewrite for small edits). |
| **Efficiency**    | Did not loop or re-read the same file repeatedly. ≤2× the minimum tool calls. |
| **Self-recovery** | When a tool errored, re-tried with corrected args (didn't ask the user).      |

Each test reports the agent's **final state**, **#tool calls**, and a **✅/⚠️/❌** judgement per rubric column.

## How to run

```bash
# 1. Generate a fresh sandbox
cd <path-to-ivy-cli>/tests
ROOT=$(./setup_fixtures.sh)
echo "sandbox at: $ROOT"

# 2. For each test, open IVY in the fixture's subdirectory and paste the prompt
cd "$ROOT/tier1" && ivy
> <paste T1.1 prompt>
> /exit

# 3. After IVY finishes, inspect the directory state vs the expected outcome.
# 4. Repeat with `claude` in the same dir for side-by-side comparison.
```

A short post-run inspection script for each test is provided under "Verify" — copy-paste into a shell to check the filesystem state.

---

## Tier 1 — Atomic tool ops

Goal: confirm each tool works in isolation. Should be near-100% pass; if any of these fail, model selection or tool schema is broken.

### T1.1 · Read a file
- **Prompt:** `Print the contents of calc.py.`
- **Expected tools:** `read_file` once.
- **Pass:** Output contains the three function bodies; no edit tools called.
- **Verify:** N/A (no filesystem change).

### T1.2 · List a directory
- **Prompt:** `List the files in this directory and tell me how many Python files there are.`
- **Expected tools:** `list_directory` or `list_files_by_extension`.
- **Pass:** Reports 2 .py files (`calc.py`, `empty.py`).
- **Verify:** N/A.

### T1.3 · Grep a definition
- **Prompt:** `Where is sub defined? Give me the file and line number.`
- **Expected tools:** `grep_directory` or `search_in_file`.
- **Pass:** Reports `calc.py:4` (or close).
- **Verify:** N/A.

### T1.4 · Write a new file
- **Prompt:** `Create hello.py that prints "hi from ivy" when run.`
- **Expected tools:** `generate_code` (recommended) then `write_file`.
- **Pass:** `hello.py` exists, runs cleanly with `python hello.py` and prints exactly `hi from ivy`.
- **Verify:** `python hello.py`

---

## Tier 2 — Multi-tool edits

Goal: confirm tool *selection* — model picks `str_replace_in_file` over a full `write_file` rewrite.

### T2.1 · Explain
- **Prompt:** `Read calc.py and summarize what each function does in one sentence each.`
- **Expected tools:** `read_file` once. No edits.
- **Pass:** Names three functions, ~accurate one-liners.
- **Verify:** `diff <(cat calc.py) <(cat /tmp/expected-T2.1-calc.py)` (no change expected).

### T2.2 · Type swap
- **Prompt:** `In calc.py, change all type annotations from int to float — both args and return types.`
- **Expected tools:** `read_file` once, then `str_replace_in_file` (or `regex_replace_in_file`) — NOT `write_file`.
- **Pass:**
  - All 9 occurrences of `int` replaced with `float`.
  - Function bodies untouched.
  - **Fail** if the agent rewrites the whole file via `write_file`.
- **Verify:**
  ```
  grep -c ': int' calc.py     # must be 0
  grep -c '-> int' calc.py    # must be 0
  grep -c ': float' calc.py   # must be 6
  grep -c '-> float' calc.py  # must be 3
  ```

### T2.3 · Add a function
- **Prompt:** `Add a divide(a, b) function to calc.py. Raise ValueError if b == 0. Keep the existing functions untouched.`
- **Expected tools:** `read_file`, then either `insert_after_line` or `str_replace_in_file` (NOT full rewrite).
- **Pass:**
  - `divide` exists, raises `ValueError` on zero divisor.
  - The three pre-existing functions are byte-identical.
- **Verify:**
  ```
  python -c "from calc import divide; assert divide(10,2)==5; \
             import calc; \
             try: divide(1,0); print('FAIL'); \
             except ValueError: print('OK')"
  diff <(head -9 calc.py) <(./setup_fixtures.sh > /dev/null && head -9 /tmp/ivy-test-*/tier2/calc.py | tail -9)
  ```

---

## Tier 3 — Realistic coding tasks

Goal: matches Claude-Code-style "agent does the whole task" expectations. Multi-step, requires planning.

### T3.1 · Bug fix
- **Setup:** `tier3/bug/factorial.py` — `factorial(5)` returns 24 (off-by-one in the loop).
- **Prompt:** `factorial(5) is returning 24 but should return 120. Find and fix the bug.`
- **Expected tools:** `read_file`, then `str_replace_in_file` on the loop range.
- **Pass:** `python factorial.py` prints `120`. Loop is `range(1, n+1)` or equivalent.
- **Fail** if agent rewrites the function unnecessarily.
- **Verify:** `python tier3/bug/factorial.py` outputs `120`.

### T3.2 · Refactor
- **Setup:** `tier3/refactor/classify.py` — deeply nested if/else.
- **Prompt:** `Refactor classify() to use early returns or a lookup. Behavior must stay identical for all input combinations.`
- **Expected tools:** `read_file`, then either a full `write_file` (acceptable here — refactor is non-local) or multiple `str_replace_in_file` calls.
- **Pass:**
  - Code passes the behavior table below (12 cases).
  - Result is visibly less nested (max indentation ≤ 2 levels).
- **Verify:** run `python -c "from classify import classify; ..."` against these 12 cases:
  ```
  classify( 95, False, False, False) == "platinum"
  classify( 95, True,  False, False) == "premium-platinum"
  classify( 95, False, True,  False) == "vip-platinum"
  classify( 95, True,  True,  False) == "vip-platinum"
  classify( 75, False, False, False) == "gold"
  classify( 75, True,  False, False) == "premium-gold"
  classify( 60, False, False, False) == "silver"
  classify( 30, False, False, False) == "bronze"
  classify( 95, False, False, True)  == "blocked"
  classify( 75, True,  True,  True)  == "blocked"
  classify(100, True,  True,  False) == "vip-platinum"
  classify(  0, False, False, False) == "bronze"
  ```

### T3.3 · Cross-file change
- **Setup:** `tier3/crossfile/main.py` prints raw names; `utils.py` has `format_name`.
- **Prompt:** `Use utils.format_name to print names in main.py instead of the raw f-string.`
- **Expected tools:** `read_file` on both files, then `str_replace_in_file` on `main.py` to (a) add the import and (b) swap the print call.
- **Pass:** `python main.py` prints:
  ```
  DELERUE, Arthur
  LOVELACE, Ada
  TURING, Alan
  ```
  And `utils.py` is unchanged.
- **Verify:** `python tier3/crossfile/main.py` matches the expected output.

---

## Tier 4 — Robustness

Goal: the agent recovers from errors and resists loops. This is where weak models fail visibly.

### T4.1 · Self-recovery from a bad path
- **Prompt:** `Read /tmp/does-not-exist-totally-fake-9001.txt and summarize it. If you can't, look in the current directory for a file called sample.py and summarize that instead.`
- **Expected tools:** `read_file` (errors) → `read_file` on `sample.py`.
- **Pass:** Agent gracefully falls through to `sample.py` and summarizes `greet`. Does NOT ask the user where the file is.
- **Verify:** N/A.

### T4.2 · Loop resistance
- **Prompt:** `Read sample.py 10 times and tell me what it says.`
- **Expected behavior:** `MAX_IDENTICAL_CALLS=2` should fire after 2 identical `read_file` calls. Agent should produce a final answer based on the read it already has.
- **Pass:** No more than 3 reads of `sample.py`. Final response describes `greet`. The loop-detection warning is acceptable.
- **Verify:** N/A.

---

## Recording results

For each row below, run the test in a fresh shell, then mark each rubric cell with **✅ / ⚠️ / ❌**. Use the comments column for tool-call counts and notes (e.g. "5 tool calls, used write_file instead of str_replace").

| #   | Test              | Correctness | Tool choice | Efficiency | Self-recovery | IVY notes | Claude Code notes |
| --- | ----------------- | ----------- | ----------- | ---------- | ------------- | --------- | ----------------- |
| 1   | T1.1 Read         |             |             |            |               |           |                   |
| 2   | T1.2 List         |             |             |            |               |           |                   |
| 3   | T1.3 Grep         |             |             |            |               |           |                   |
| 4   | T1.4 Write        |             |             |            |               |           |                   |
| 5   | T2.1 Explain      |             |             |            |               |           |                   |
| 6   | T2.2 Type swap    |             |             |            |               |           |                   |
| 7   | T2.3 Add function |             |             |            |               |           |                   |
| 8   | T3.1 Bug fix      |             |             |            |               |           |                   |
| 9   | T3.2 Refactor     |             |             |            |               |           |                   |
| 10  | T3.3 Cross-file   |             |             |            |               |           |                   |
| 11  | T4.1 Recovery     |             |             |            |               |           |                   |
| 12  | T4.2 Loops        |             |             |            |               |           |                   |

## What the result tells you

- **≥9 / 12 correctness ✅**: IVY's coding agent is broadly usable. Gaps will be on Tier 3 (long-context planning).
- **Tool-choice column dominated by ❌ on T2.x / T3.x**: the model rewrites files instead of using surgical edits — system prompt or model swap.
- **Efficiency ❌ on Tier 3**: weak planning — try `qwen3:14b` as principal or add the self-correction nudges layer.
- **Self-recovery ❌ on T4.1**: model gives up on first tool error instead of re-trying — needs the self-correction-nudge layer we discussed.

Re-run after each significant change (model swap, system prompt tweak, new tool) to see the regression delta.
