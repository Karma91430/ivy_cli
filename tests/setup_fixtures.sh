#!/usr/bin/env bash
# Generates a fresh sandbox of test fixtures for the IVY coding-agent protocol.
# Usage:  ./setup_fixtures.sh   (creates /tmp/ivy-test-<timestamp>/ and prints the path)

set -e

STAMP="$(date +%Y%m%d-%H%M%S)"
ROOT="/tmp/ivy-test-${STAMP}"
mkdir -p "$ROOT"
cd "$ROOT"

# ───── T1 atomic ─────
mkdir -p tier1
cat > tier1/calc.py <<'EOF'
def add(a, b):
    return a + b

def sub(a, b):
    return a - b

def mul(a, b):
    return a * b
EOF

cat > tier1/notes.md <<'EOF'
# Project notes

## Install
Run `pip install -r requirements.txt` and then `python main.py`.

## Architecture
Single-file CLI. State lives in `state.json`.
EOF

# A few extra files so directory listing has signal
touch tier1/empty.py tier1/keep.txt
mkdir -p tier1/sub
echo "marker" > tier1/sub/inner.txt


# ───── T2 multi-tool ─────
mkdir -p tier2
cat > tier2/calc.py <<'EOF'
def add(a: int, b: int) -> int:
    return a + b

def sub(a: int, b: int) -> int:
    return a - b

def mul(a: int, b: int) -> int:
    return a * b
EOF


# ───── T3 realistic ─────
mkdir -p tier3/bug tier3/refactor tier3/crossfile

# T3.1 — off-by-one bug
cat > tier3/bug/factorial.py <<'EOF'
def factorial(n: int) -> int:
    """Return n! — but there's a bug somewhere."""
    if n <= 0:
        return 1
    result = 1
    for i in range(1, n):     # ← off-by-one: should be range(1, n+1)
        result *= i
    return result


if __name__ == "__main__":
    print(factorial(5))       # currently prints 24, should print 120
EOF

# T3.2 — refactor target (deeply nested)
cat > tier3/refactor/classify.py <<'EOF'
def classify(score: int, premium: bool, vip: bool, suspended: bool) -> str:
    if suspended:
        return "blocked"
    else:
        if score >= 90:
            if vip:
                return "vip-platinum"
            else:
                if premium:
                    return "premium-platinum"
                else:
                    return "platinum"
        else:
            if score >= 70:
                if premium:
                    return "premium-gold"
                else:
                    return "gold"
            else:
                if score >= 50:
                    return "silver"
                else:
                    return "bronze"
EOF

# T3.3 — cross-file: utils exposes format_name, main.py prints names raw
cat > tier3/crossfile/utils.py <<'EOF'
def format_name(first: str, last: str) -> str:
    """Return 'Last, First' — canonical display form used across the app."""
    return f"{last.upper()}, {first.title()}"
EOF
cat > tier3/crossfile/main.py <<'EOF'
USERS = [("arthur", "delerue"), ("ada", "lovelace"), ("alan", "turing")]


def main():
    for first, last in USERS:
        print(f"{first} {last}")


if __name__ == "__main__":
    main()
EOF


# ───── T4 robustness ─────
mkdir -p tier4
cat > tier4/sample.py <<'EOF'
def greet(name: str) -> str:
    return f"hello {name}"
EOF


echo "$ROOT"
