/**
 * theme.ts — claude-tmux state palette (mirrors plugin theme.json) + Nord canvas tokens.
 */

export const STATE_COLORS: Record<string, string> = {
  idle: "#89b4fa",
  unread: "#b4befe",
  working: "#a6e3a1",
  agents: "#cba6f7",
  asking: "#f9e2af",
  error: "#f38ba8",
  nudge: "#6699cc",
};

export function stateColor(state: string | undefined, fallback = "#6c7086"): string {
  return (state && STATE_COLORS[state]) || fallback;
}

/** Nord-ish dark canvas tokens. */
export const C = {
  bg: "#1e1e2e",
  bgElev: "#181825",
  panel: "#11111b",
  border: "#313244",
  borderActive: "#585b70",
  text: "#cdd6f4",
  textDim: "#9399b2",
  textFaint: "#6c7086",
} as const;
