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
# Test hooks: AGENT_DESKTOP_SETUP_PACMAN replaces /usr/bin/pacman,
# AGENT_DESKTOP_SETUP_USER_RUNTIME replaces /run/user/UID (the user manager's
# runtime directory) and AGENT_DESKTOP_SETUP_LOCK_WAIT the install-lock wait
# (default 60s), to exercise the failure messages.
set -euo pipefail

PROJECT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
ROOT="$PROJECT/.local/dependencies"
VENV="$ROOT/venv"
CLI="$VENV/bin/agent-desktop"
STAMP="$VENV/.agent-desktop-install.sha256"
PYTHON=/usr/bin/python
PACMAN=${AGENT_DESKTOP_SETUP_PACMAN:-/usr/bin/pacman}
RUSTUP=/usr/bin/rustup
USER_RUNTIME=${AGENT_DESKTOP_SETUP_USER_RUNTIME:-/run/user/$(id -u)}
# Shared by `session start` (src/agent_desktop/lifecycle.py install_lock_path).
INSTALL_LOCK="$USER_RUNTIME/agent-desktop/install.lock"
LOCK_WAIT=${AGENT_DESKTOP_SETUP_LOCK_WAIT:-60}

case "${1:-}" in
    "") ;;
    -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "usage: tools/setup.sh" >&2; exit 2 ;;
esac

say() { printf 'setup: %s\n' "$*"; }
fail() { printf 'setup: FAILED: %s\n' "$*" >&2; exit 1; }

# Running agent-desktop session services, from the same user manager that the
# lifecycle code addresses (src/agent_desktop/lifecycle.py). Fails closed: a
# failed query never counts as "no sessions".
live_sessions() {
    local out
    if ! out=$(env -i PATH=/usr/bin:/bin LANG=C.UTF-8 XDG_RUNTIME_DIR="$USER_RUNTIME" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=$USER_RUNTIME/bus" \
            /usr/bin/systemctl --user --no-ask-password list-units --plain --no-legend \
            'agent-desktop-*.service' 2>&1); then
        fail "cannot query the systemd user manager at unix:path=$USER_RUNTIME/bus:
$out
Setup only changes the installed package after confirming no session is running.
Run it from a normal login session with a running user manager and retry."
    fi
    cut -d' ' -f1 <<<"$out" | sed '/^$/d'
}

refuse_if_live() {  # $1: what would change
    local live
    live=$(live_sessions) || exit 1
    if [[ -n $live ]]; then
        fail "agent-desktop sessions are running and $1:
$live
Their stop hooks run the installed code. Stop them first
(agent-desktop session stop --session NAME), then rerun tools/setup.sh."
    fi
}

# Exclusive install lock: a `session start` holds it shared while it launches a
# worker, so it never imports a half-installed package, and setup never changes
# the package under a starting session.
take_install_lock() {
    local dir=${INSTALL_LOCK%/*}
    [[ -d $USER_RUNTIME && ! -L $USER_RUNTIME ]] || fail "$USER_RUNTIME does not exist; the systemd user manager is not running.
Run setup from a normal login session."
    [[ -e $dir ]] || mkdir -m 700 -- "$dir" 2>/dev/null || [[ -d $dir ]] \
        || fail "cannot create $dir"
    [[ -d $dir && ! -L $dir && $(stat -c '%u %a' -- "$dir") == "$(id -u) 700" ]] \
        || fail "$dir must be a directory owned by you with mode 0700."
    [[ ! -L $INSTALL_LOCK ]] || fail "$INSTALL_LOCK is a symlink; remove it."
    (umask 077 && : >>"$INSTALL_LOCK") || fail "cannot create $INSTALL_LOCK"
    [[ $(stat -c '%u %a' -- "$INSTALL_LOCK") == "$(id -u) 600" ]] \
        || fail "$INSTALL_LOCK must be a file owned by you with mode 0600."
    exec {LOCK_FD}<"$INSTALL_LOCK"
    if ! flock -x -n "$LOCK_FD"; then
        say "waiting up to ${LOCK_WAIT}s for a session start to finish (install lock $INSTALL_LOCK)"
        flock -x -w "$LOCK_WAIT" "$LOCK_FD" || fail "a session start still holds $INSTALL_LOCK after ${LOCK_WAIT}s.
Let it finish (or stop that session), then rerun tools/setup.sh."
    fi
}

release_install_lock() { exec {LOCK_FD}<&-; }

# Interpreter identity of the venv: full version and the resolved executable.
venv_identity() {
    "$VENV/bin/python" -I -c 'import os, sys; print(sys.version.replace("\n", " "), os.path.realpath(sys.executable))' 2>/dev/null
}

# Health check: the package imports from inside the venv. Prints its location.
venv_health() {
    local file
    file=$(cd / && "$VENV/bin/python" -I -c 'import os, agent_desktop; print(os.path.realpath(agent_desktop.__file__))' 2>/dev/null) \
        || return 1
    [[ $file == "$(realpath "$VENV")"/* ]] || return 1
    printf '%s\n' "$file"
}

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
# Everything that changes the venv or the installed package runs under the
# exclusive install lock; it is released before doctor.
take_install_lock
# A venv whose interpreter no longer runs, or now runs a different Python minor
# version than it was created for (an Arch Python upgrade), cannot be repaired by
# pip. Recreate only the venv; the kdotool build next to it is kept.
if [[ -e $VENV ]]; then
    created=$(sed -n 's/^version *= *\([0-9]*\.[0-9]*\).*/\1/p' "$VENV/pyvenv.cfg" 2>/dev/null || true)
    actual=$("$VENV/bin/python" -I -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)
    if [[ -z $actual || $created != "$actual" ]]; then
        refuse_if_live "the venv must be recreated"
        say "recreating $VENV (created for Python ${created:-unknown}, interpreter is ${actual:-not runnable})"
        rm -rf -- "$VENV"
    fi
fi
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
identity=$(venv_identity) || fail "the venv interpreter $VENV/bin/python does not run.
Delete $VENV and rerun tools/setup.sh."
expected="$source_hash $identity"
if [[ -x $CLI && -f $STAMP && $(<"$STAMP") == "$expected" ]] && venv_health >/dev/null; then
    say "agent-desktop already installed from these sources"
else
    refuse_if_live "the installed package would change"
    say "installing agent-desktop into $VENV"
    rm -f "$STAMP"
    # -I does not isolate pip: drop every PIP_* setting and all pip config files
    # so nothing can redirect the install (--target, --prefix, --user, --root).
    pip_unset=()
    while IFS= read -r name; do pip_unset+=(-u "$name"); done < <(compgen -e | grep '^PIP_' || true)
    env "${pip_unset[@]}" PIP_CONFIG_FILE=/dev/null \
        "$VENV/bin/python" -I -m pip install --isolated --no-user --quiet \
        --disable-pip-version-check --no-input "$PROJECT" \
        || fail "pip install failed (it needs network access for the setuptools build backend)."
    location=$(venv_health) || fail "the installed package does not import from $VENV.
Delete $VENV and rerun tools/setup.sh."
    say "installed: $location"
    # Belt and braces: session start waits for the install lock, so this should
    # never find anything.
    late=$(live_sessions) || exit 1
    if [[ -n $late ]]; then
        fail "agent-desktop sessions started while the package was being installed:
$late
Stop them (agent-desktop session stop --session NAME) and rerun tools/setup.sh."
    fi
    printf '%s\n' "$expected" >"$STAMP"
fi

release_install_lock

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
