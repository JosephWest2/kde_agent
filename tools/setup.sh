#!/usr/bin/env bash
# One-command repository setup for agent-desktop (docs/SETUP.md).
#
#   tools/setup.sh
#
# 1. Checks the Arch packages in dependencies.json and the rustup stable toolchain.
# 2. Runs tools/dependencies.py setup: the venv (--system-site-packages) and the
#    pinned kdotool build in .local/dependencies, verified on reruns.
# 3. pip-installs this checkout into that venv, skipped when the sources are unchanged.
# 4. Runs `agent-desktop doctor`.
#
# Never runs sudo or installs system packages: a missing prerequisite is printed
# with the command to install it, and the script exits 1. Rerunning is a quick no-op.
# AGENT_DESKTOP_SETUP_PACMAN overrides /usr/bin/pacman, for testing the
# missing-prerequisite messages.
set -euo pipefail

PROJECT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
ROOT="$PROJECT/.local/dependencies"
VENV="$ROOT/venv"
CLI="$VENV/bin/agent-desktop"
STAMP="$VENV/.agent-desktop-install.sha256"
PYTHON=/usr/bin/python
PACMAN=${AGENT_DESKTOP_SETUP_PACMAN:-/usr/bin/pacman}
RUSTUP=/usr/bin/rustup

case "${1:-}" in
    "") ;;
    -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "usage: tools/setup.sh" >&2; exit 2 ;;
esac

say() { printf 'setup: %s\n' "$*"; }
fail() { printf 'setup: FAILED: %s\n' "$*" >&2; exit 1; }

# --- Host prerequisites (read-only checks) -----------------------------------
[[ -x $PACMAN ]] || fail "$PACMAN not found. agent-desktop supports Arch Linux with KDE Plasma 6 only."
[[ -x $PYTHON ]] || fail "$PYTHON not found. Install it with: sudo pacman -S --needed python"

mapfile -t packages < <("$PYTHON" -I -c 'import json, sys
print("\n".join(json.load(open(sys.argv[1]))["arch_packages"]))' "$PROJECT/dependencies.json")
missing=$("$PACMAN" -T "${packages[@]}" || true)
if [[ -n $missing ]]; then
    missing=$(tr '\n' ' ' <<<"$missing" | sed 's/ *$//')
    fail "missing Arch packages: $missing
Install them with:
    sudo pacman -S --needed $missing
then rerun tools/setup.sh."
fi

if ! RUSTUP_HOME="$HOME/.rustup" RUSTUP_TOOLCHAIN=stable RUSTUP_AUTO_INSTALL=0 \
        "$RUSTUP" which cargo >/dev/null 2>&1; then
    fail "the rustup stable toolchain is not installed (needed to build the pinned kdotool).
Install it with:
    rustup toolchain install stable
then rerun tools/setup.sh."
fi

# --- Pinned dependencies and venv --------------------------------------------
if [[ -f $ROOT/build.json ]]; then
    say "verifying pinned dependencies in $ROOT"
else
    say "building pinned dependencies in $ROOT (first run: fetches and builds kdotool, needs network)"
fi
report=$(mktemp)
trap 'rm -f "$report"' EXIT
if ! "$PYTHON" -I "$PROJECT/tools/dependencies.py" setup --root "$ROOT" >"$report"; then
    "$PYTHON" -I - "$report" >&2 <<'EOF'
import json, sys
data = json.load(open(sys.argv[1]))
print("setup: FAILED: tools/dependencies.py setup reported:")
for error in data.get("errors", []):
    print(f"  - {error.get('message')} ({error.get('code')})")
    if error.get("repair"):
        print(f"    repair: {error['repair']}")
    if error.get("diagnostic"):
        print("    " + error["diagnostic"].strip().replace("\n", "\n    "))
print("Rerun after fixing; full JSON: /usr/bin/python -I tools/dependencies.py report")
EOF
    exit 1
fi

# --- Install the package into the venv ---------------------------------------
source_hash=$(cd "$PROJECT" && find pyproject.toml src -type f \
        ! -path '*/__pycache__/*' ! -path '*.egg-info/*' ! -name '*.pyc' -print0 \
    | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
if [[ -x $CLI && -f $STAMP && $(<"$STAMP") == "$source_hash" ]]; then
    say "agent-desktop already installed from these sources"
else
    # Hooks of a live session run the installed code (docs/APPLICATIONS.md).
    live=$(systemctl --user list-units --plain --no-legend 'agent-desktop-*.service' 2>/dev/null \
        | cut -d' ' -f1 || true)
    if [[ -n $live ]]; then
        fail "agent-desktop sessions are running and the installed package would change:
$live
Stop them first (agent-desktop session stop --session NAME), then rerun tools/setup.sh."
    fi
    say "installing agent-desktop into $VENV"
    rm -f "$STAMP"
    "$VENV/bin/python" -I -m pip install --quiet --disable-pip-version-check --no-input "$PROJECT" \
        || fail "pip install failed (it needs network access for the setuptools build backend)."
    printf '%s\n' "$source_hash" >"$STAMP"
fi

# --- Diagnose -----------------------------------------------------------------
doctor=$(cd "$PROJECT" && "$CLI" --json doctor --dependency-root "$ROOT") || true
"$VENV/bin/python" -I - "$doctor" <<'EOF' || exit 1
import json, sys
try:
    data = json.loads(sys.argv[1])
except ValueError:
    print("setup: FAILED: agent-desktop doctor produced no JSON result", file=sys.stderr)
    raise SystemExit(1)
if data.get("ok"):
    for warning in data["result"].get("warnings", []):
        print(f"setup: warning: {warning['code']}: {warning['observed']} (tested {warning['tested']}). {warning['advice']}")
    raise SystemExit(0)
error = data.get("error", {})
context = error.get("context", {})
print(f"setup: FAILED: agent-desktop doctor: {error.get('code')}: {error.get('message')}", file=sys.stderr)
for item in context.get("prerequisite_report", {}).get("dependencies", []):
    if item["status"] == "failed":
        print(f"  - {item['name']}: {item['reason']}\n    repair: {item['repair']}", file=sys.stderr)
raise SystemExit(1)
EOF

say "OK: agent-desktop is installed and doctor passed."
cat <<EOF
Next steps (from $PROJECT; elsewhere add --dependency-root $ROOT):
    $CLI --json session start
    $CLI --json launch --wait-window -- gnome-text-editor
Add $VENV/bin to PATH to run plain \`agent-desktop\`.
README.md has the agent quickstart. To verify the desktop end to end:
    python tests/integration/smoke.py --cli $CLI
EOF
