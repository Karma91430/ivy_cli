# IVY Coding-Agent Tests

A reproducible protocol for evaluating IVY's coding agent against Claude Code.

```bash
# 1. Generate a fresh sandbox of fixtures
./setup_fixtures.sh
# → prints e.g. /tmp/ivy-test-20260520-005700

# 2. Read the protocol
open protocol.md          # 12 tests across 4 tiers + grading rubric

# 3. For each test: cd into the right tier dir, run `ivy`, paste the prompt
#    Then repeat with `claude` for side-by-side comparison.
```

The grading sheet at the end of `protocol.md` is what you fill in.
