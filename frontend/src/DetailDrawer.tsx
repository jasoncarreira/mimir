import React from "react";

// Right-side pop-out detail panel (dimmed backdrop + Escape-to-close). Shown
// only when `open`; used by list views that reveal a record's detail on click
// so the detail comes to you instead of living in a panel you scroll down to.
export function DetailDrawer({
  title,
  open,
  onClose,
  children
}: {
  title: React.ReactNode;
  open: boolean;
  onClose: () => void;
  children: React.ReactNode;
}) {
  React.useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <>
      <button
        aria-label="Dismiss details"
        className="detail-drawer__backdrop"
        onClick={onClose}
        type="button"
      />
      <aside aria-label="Details" className="detail-drawer">
        <div className="detail-drawer__head">
          <span className="detail-drawer__title">{title}</span>
          <button
            aria-label="Close details"
            className="detail-drawer__close"
            onClick={onClose}
            type="button"
          >
            ×
          </button>
        </div>
        <div className="detail-drawer__body">{children}</div>
      </aside>
    </>
  );
}
