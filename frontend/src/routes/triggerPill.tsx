// Normalize a raw trigger string to a stable token for CSS color mapping and
// the data-trigger attribute (e.g. "saga_session_end", "scheduled_tick").
export function normalizeTrigger(trigger: string): string {
  const token = (trigger || "unknown")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return token || "unknown";
}

// A color-coded trigger pill. Distinct colors per trigger type make the turn
// list scannable (github #570). Colors are keyed on data-trigger in CSS;
// unrecognized triggers fall back to the neutral style.
export function TriggerPill({ trigger }: { trigger: string }) {
  const label = trigger || "unknown";
  return (
    <span className="turn-trigger" data-trigger={normalizeTrigger(label)}>
      {label}
    </span>
  );
}
