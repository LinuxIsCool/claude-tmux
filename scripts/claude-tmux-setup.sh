#!/usr/bin/env bash
# Inject claude-tmux indicator into tmux format strings
# Run once after tmux-power or theme plugin loads
#
# Architecture: hooks write to tmux pane options (@claude_state, @claude_color, @claude_persona).
# Format strings read them with #{@claude_state}, #[fg=#{@claude_color}], #{@claude_persona}.
# Conditional #{?...} hides the indicator when no state is set.

CURRENT_WSF=$(tmux show-option -gv window-status-format 2>/dev/null)

# --- Start tmux-arbiter if not running ---
# Must run unconditionally — even if format strings are already injected,
# the arbiter needs to be running for indicators to work.
if ! systemctl --user is-active --quiet claude-tmux-arbiter.service 2>/dev/null; then
    # Keep an installed unit under systemd ownership, including failed/masked
    # units. A start failure must not create an unmanaged competing launcher.
    ARBITER_UNIT_INSTALLED=0
    ARBITER_LOAD_STATE=$(systemctl --user show --property=LoadState --value claude-tmux-arbiter.service 2>/dev/null) || ARBITER_LOAD_STATE=""
    case "$ARBITER_LOAD_STATE" in
        ""|not-found) ;;
        *) ARBITER_UNIT_INSTALLED=1 ;;
    esac
    for unit_dir in "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user" \
        "${XDG_DATA_HOME:-$HOME/.local/share}/systemd/user" \
        "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/systemd/user" \
        /etc/systemd/user /run/systemd/user /usr/local/lib/systemd/user /usr/lib/systemd/user; do
        if [ -e "$unit_dir/claude-tmux-arbiter.service" ] || [ -L "$unit_dir/claude-tmux-arbiter.service" ]; then
            ARBITER_UNIT_INSTALLED=1
        fi
    done
    if [ "$ARBITER_UNIT_INSTALLED" = "1" ]; then
        systemctl --user start claude-tmux-arbiter.service >/dev/null 2>&1 || true
    else
        ARBITER="$(cd "$(dirname "$0")" && pwd)/tmux-arbiter.py"
        if [ -f "$ARBITER" ] && command -v uv >/dev/null 2>&1; then
            uv run "$ARBITER" 2>/dev/null &
        fi
    fi
fi

# --- Format string injection (idempotent: strips old, injects new) ---
THEME_FILE="$(cd "$(dirname "$0")/.." && pwd)/theme.json"

# Build the legacy title decorations. Pane options remain available to voice,
# focus and other consumers, but these decorations are not window-tab content.
# Build segment fragments from theme.json segment_order array.
# Each segment maps to a tmux user option: @claude_{segment} / @claude_window_{segment}
# Default order if theme.json missing: persona voice hook
build_segments() {
    local prefix="$1"  # "" for pane-level, "window_" for window-level
    local fg_restore="$2"  # color to restore after state dot
    local voice_pane_first="$3"  # "1" => prefer this pane's own @claude_voice
                                  # (window-status-current-format only); unset
                                  # (or anything else) => prefer the window
                                  # aggregate, @claude_window_voice

    local segments=""
    if [ -f "$THEME_FILE" ] && command -v jq >/dev/null 2>&1; then
        segments=$(jq -r '.segment_order // ["persona","voice","hook"] | .[]' "$THEME_FILE" 2>/dev/null)
    fi
    [ -z "$segments" ] && segments="persona voice hook"

    local result=""
    for seg in $segments; do
        local var="@claude_${prefix}${seg}"
        if [ "$seg" = "voice" ]; then
            # Lifecycle segments use the winning sibling, but voice continues
            # to follow the voice arbiter's own pane/window selection rules.
            var="@claude_voice"
            # window-status-format renders in the ACTIVE pane's context. Voice
            # is a window-scoped question for every OTHER window in the status
            # bar ("is anyone in this tab talking?"), so window-status-format
            # keeps preferring the aggregate @claude_window_voice (mirrored by
            # voice_arbiter.py) and falls back to this pane's own option when
            # unset (found 2026-08-05).
            #
            # But for the CURRENT window — the one you're looking at —
            # rendering always resolves against the ACTIVE pane. Preferring
            # the aggregate there means every pane you select shows the same
            # window-wide star, so you can't tell which pane is actually
            # speaking. window-status-current-format instead prefers the
            # active pane's own @claude_voice first, falling back to the
            # window aggregate only when this pane has no voice state of its
            # own (found 2026-08-06).
            if [ "$voice_pane_first" = "1" ]; then
                result="${result}#{?#{${var}},#{${var}},#{?#{@claude_window_voice},#{@claude_window_voice},}}"
            else
                result="${result}#{?#{@claude_window_voice},#{@claude_window_voice},#{?#{${var}},#{${var}},}}"
            fi
        elif [ "$seg" = "agents" ]; then
            local agents_color
            agents_color=$(jq -r '.semantic_colors[.states.agents.semantic] // "#cba6f7"' "$THEME_FILE" 2>/dev/null)
            result="${result}#{?#{${var}},#[fg=${agents_color:-#cba6f7}]#{${var}}#[fg=${fg_restore}],}"
        else
            result="${result}#{?#{${var}},#{${var}},}"
        fi
    done
    echo "#[fg=#{@claude_${prefix}color}]#{@claude_${prefix}state}#[fg=${fg_restore}]${result} "
}

# Tabs have one lifecycle badge. Native state names whitelist the approved
# grammar so stale persona/tool/unknown glyphs can never leak into the tab.
build_tab_marker() {
    local state="#{@claude_${1}state_name}"
    local glyph="#{@claude_${1}state}"
    local named="#{?#{==:${state},idle},🔵,#{?#{==:${state},unread},🔵,#{?#{==:${state},nudge},🔵,#{?#{==:${state},asking},🔵,#{?#{==:${state},working},🟢,#{?#{==:${state},agents},💠,}}}}}}"
    local fallback="#{?#{==:${glyph},🔵},🔵,#{?#{==:${glyph},🟢},🟢,#{?#{==:${glyph},💠},💠,}}}"
    echo "#{?${state},${named},${fallback}} "
}

PANE_IND=$(build_tab_marker "")
WIN_IND=$(build_tab_marker "window_")
INDICATOR="#{?#{@claude_window_state_name},${WIN_IND},#{?#{@claude_window_state},${WIN_IND},#{?#{@claude_state},${PANE_IND}, }}}"

# Strip either the old pane-first or current window-first indicator before #I.
# Nested #{...} makes regex unreliable, so find the marker and splice.
strip_claude_indicator() {
    python3 -c "
import sys
s = sys.stdin.read()
markers = ('#{?#{@claude_state}', '#{?#{@claude_window_state}', '#{?#{@claude_window_state_name}')
indices = [s.find(marker) for marker in markers if marker in s]
idx = min(indices) if indices else -1
if idx < 0:
    print(s, end='')
else:
    suffix_marker = '#I'
    suffix_idx = s.rfind(suffix_marker)
    if suffix_idx > idx:
        print(s[:idx] + s[suffix_idx:], end='')
    else:
        print(s, end='')
" <<< "$1"
}

# Get current formats (may contain old indicators)
CURRENT_WSCF=$(tmux show-option -gv window-status-current-format 2>/dev/null)

# Strip old indicators to get clean base
BASE_WSF=$(strip_claude_indicator "$CURRENT_WSF")
BASE_WSCF=$(strip_claude_indicator "$CURRENT_WSCF")

# For CURRENT window: extract theme's text color and restore to THAT instead of default
CURRENT_FG=$(echo "$BASE_WSCF" | sed 's/#I.*//' | grep -oP 'fg=#[0-9a-fA-F]{6}' | tail -1 | cut -d= -f2)
CURRENT_FG="${CURRENT_FG:-default}"

INDICATOR_CURRENT="$INDICATOR"

# Insert before #I; strip #F (last-window flag is visual noise)
NEW_WSF=$(echo "$BASE_WSF" | sed "s|#F||" | sed "s|#I|${INDICATOR}#I|")
NEW_WSCF=$(echo "$BASE_WSCF" | sed "s|#F||" | sed "s|#I|${INDICATOR_CURRENT}#I|")

if [ "$NEW_WSF" = "$BASE_WSF" ]; then
    echo "claude-tmux: WARNING — #I not found in window-status-format" >&2
    exit 1
fi

# Apply format strings immediately (in-memory)
tmux set -g window-status-format "$NEW_WSF"
tmux set -g window-status-current-format "$NEW_WSCF"
tmux set -g status-fg "#8a8a8a"

# Keep Shawn's full date/time widgets, replacing only the cached strftime
# expansions with the existing clock helper's fresh local-time processes.
CLOCK_SCRIPT="$HOME/.config/tmux/scripts/status-clock.sh"
CLOCK_RIGHT=""
if [ -x "$CLOCK_SCRIPT" ]; then
    CURRENT_SR=$(tmux show-option -gv status-right 2>/dev/null)
    CLOCK_RIGHT=$(python3 - "$CLOCK_SCRIPT" "$CURRENT_SR" <<'PY'
import shlex
import sys
command = shlex.quote(sys.argv[1])
print(sys.argv[2].replace('%A %B %d %F', '#(' + command + ' date)')
      .replace('%H:%M %Z', '#(' + command + ' time)'), end='')
PY
)
    tmux set -g status-right "$CLOCK_RIGHT"
fi

# --- Session persona icon in status-left ---
if [ -f "$THEME_FILE" ] && command -v jq >/dev/null 2>&1; then
    CURRENT_SL=$(tmux show-option -gv status-left 2>/dev/null)
    if ! echo "$CURRENT_SL" | grep -q "@session_icon"; then
        if echo "$CURRENT_SL" | grep -q '✨'; then
            NEW_SL=$(echo "$CURRENT_SL" | sed 's|✨|#{?#{@session_icon},#{@session_icon},✨}|')
            tmux set -g status-left "$NEW_SL"
        fi
        bash "$(dirname "$0")/claude-tmux-session-icons.sh" 2>/dev/null
    fi
fi

# Terminal title (for Alacritty/WezTerm window title bars)
if [ "$(tmux show-option -gqv @claude_tmux_set_titles 2>/dev/null)" != "off" ]; then
    TITLE_PANE_IND=$(build_segments "" "default")
    TITLE_WIN_IND=$(build_segments "window_" "default")
    TITLE_INDICATOR="#{?#{@claude_window_state},${TITLE_WIN_IND},#{?#{@claude_state},${TITLE_PANE_IND}, }}"
    tmux set -g set-titles on
    tmux set -g set-titles-string "#S | ${TITLE_INDICATOR}#I:#W"
fi

# --- Write persistent dynamic config ---
# Survives chezmoi apply + tmux source-file. Written AFTER all injections.
DYNAMIC_CONF="$HOME/.config/tmux/plugins-dynamic.conf"
FINAL_SL=$(tmux show-option -gv status-left 2>/dev/null)
cat > "$DYNAMIC_CONF" << DYNEOF
# Generated by claude-tmux-setup.sh — do not edit manually
# Regenerated on every tmux startup or theme change
# chezmoi ignores this file (.chezmoiignore)
set -g window-status-format "$NEW_WSF"
set -g window-status-current-format "$NEW_WSCF"
set -g status-fg "#8a8a8a"
set -g status-left "$FINAL_SL"
DYNEOF
if [ -n "$CLOCK_RIGHT" ]; then
    python3 - "$CLOCK_RIGHT" >> "$DYNAMIC_CONF" <<'PY'
import json
import sys
print('set -g status-right ' + json.dumps(sys.argv[1], ensure_ascii=False))
PY
fi

# --- END format string injection ---

# --- Hook re-registration helper ---
# `tmux set-hook -gu <event>` (no index) unsets EVERY hook registered for
# that event, from every plugin sharing it — not just claude-tmux's own.
# claude-voice registers a focus hook (tmux_focus_hook.sh) on
# after-select-pane / after-select-window / client-focus-in to track which
# pane the operator is looking at, so the voice arbiter knows where to route
# audio. A blanket unset here silently deletes claude-voice's entry with no
# error and no log line — focus mode is left watching a stale/wrong pane
# while playback goes nowhere. This exact failure cost an evening to
# diagnose (2026-08-06). Do not "simplify" this back to a bare
# `set-hook -gu <event>`.
#
# Fix: read `tmux show-hooks -g`, find only the index(es) whose command
# references OUR OWN script (by basename, so a stale path from a prior
# install location still gets cleaned up — same idempotency goal as the old
# unconditional unset), and unset just those before appending the current
# command. Any other plugin's hook on the same event is left untouched.
reinstall_own_hook() {
    local event="$1" cmd="$2" script_path="$3" script_basename
    script_basename="$(basename "$script_path")"
    local idx
    for idx in $(tmux show-hooks -g 2>/dev/null \
                 | grep -F "$script_basename" \
                 | grep -oP "^${event}\[\K[0-9]+(?=\])"); do
        tmux set-hook -gu "${event}[${idx}]" 2>/dev/null
    done
    tmux set-hook -ga "$event" "$cmd" 2>/dev/null
}

# --- Split-pane indicator preservation ---
# Focus and pane-removal hooks share the arbiter's window aggregation rules.
# The lifecycle aggregate wins over the selected pane's state.
WINDOW_STATE_SCRIPT="$(cd "$(dirname "$0")" && pwd)/tmux-window-state.sh"
if [ -f "$WINDOW_STATE_SCRIPT" ]; then
    # after-select-pane: fires on pane switch within a window
    reinstall_own_hook after-select-pane "run-shell -b '$WINDOW_STATE_SCRIPT'" "$WINDOW_STATE_SCRIPT"
    # after-select-window: fires on window switch — needed for unread→read
    # (visiting an unread window via Ctrl-B N doesn't trigger after-select-pane)
    reinstall_own_hook after-select-window "run-shell -b '$WINDOW_STATE_SCRIPT'" "$WINDOW_STATE_SCRIPT"
    # A removed winning pane must immediately reveal the next live sibling.
    for evt in after-kill-pane pane-exited; do
        reinstall_own_hook "$evt" "run-shell -b '$WINDOW_STATE_SCRIPT #{window_id}'" "$WINDOW_STATE_SCRIPT"
    done
fi

# Self-healing: clear stale indicators when a pane's process dies
# Uses tmux native conditional — only fires if the dying pane had an indicator
# Also notifies voice arbiter to clean up voice state for the dead pane
tmux set-hook -g pane-died 'if -F "#{@claude_state}" "set -pu @claude_state ; set -pu @claude_state_name ; set -pu @claude_color ; set -pu @claude_persona ; set -pu @claude_hook ; set -pu @claude_focus ; set -pu @claude_agents ; run-shell -b \"echo {\\\"type\\\":\\\"pane_died\\\",\\\"pane_id\\\":\\\"#{pane_id}\\\"} | socat -t0.1 - UNIX-CONNECT:$HOME/.claude/local/voice/arbiter.sock 2>/dev/null || true\""'

if [ -f "$WINDOW_STATE_SCRIPT" ]; then
    reinstall_own_hook pane-died "run-shell -b '$WINDOW_STATE_SCRIPT #{window_id}'" "$WINDOW_STATE_SCRIPT"
fi

# --- Layout-change re-save (Fix 5, 2026-04-27) -----------------------------
# Position-keyed state files (legion:5.1.state) drift whenever windows OR
# panes are added/removed/swapped. Without re-save, state files at old
# positions go stale and post-reboot restore falls through to the fallback
# chain. These hooks call claude-resave-state.sh to refresh state files at
# current geometry. The dedupe in save_session_state collapses old keys.
#
# Hook list covers both window-level events and pane-level events:
#   - Window: kill, renumber, rename, swap, move, link, unlink
#   - Pane:   split-window (creates pane), pane-exited (kills pane)
# Pane-level events matter because pane_index renumbers when sibling panes
# are added/removed within the same window.
#
# Re-registration on every run (see rationale on reinstall_own_hook above):
# the previous marker-option guard (@claude_resave_hooks_installed) recorded
# only that registration had happened once, never whether the registered
# path was still valid, so it masked a stale post-graduation path
# indefinitely. We still want that idempotency, but via reinstall_own_hook
# rather than a blanket `set-hook -gu <event>` — any of these nine events
# could just as easily end up shared with another plugin's hook later, and
# a bare unset would delete it exactly the way it deleted claude-voice's
# focus hook on after-select-pane/-window (see above). Some event names
# below are not recognized by every tmux version ("invalid option"); that's
# expected and harmless, so stderr is discarded throughout.
RESAVE_SCRIPT="$(cd "$(dirname "$0")" && pwd)/claude-resave-state.sh"
if [ -f "$RESAVE_SCRIPT" ]; then
    for evt in after-kill-window after-renumber-window after-rename-window \
               after-swap-window after-move-window after-link-window \
               after-unlink-window after-split-window pane-exited; do
        reinstall_own_hook "$evt" "run-shell -b '$RESAVE_SCRIPT'" "$RESAVE_SCRIPT"
    done
fi
