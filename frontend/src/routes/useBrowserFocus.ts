import React from "react";
import { useLocation } from "react-router-dom";

// Keep navigation mounted: its scroll position, filters and expanded nodes survive
// reader visits. URL state makes the same focus journey work with history traversal.
export function useBrowserFocus(showDetail: boolean) {
  const root = React.useRef<HTMLDivElement>(null);
  const location = useLocation();
  const origin = React.useRef<HTMLElement | null>(null);
  const previousKey = React.useRef(location.key);

  React.useEffect(() => {
    const navigating = previousKey.current !== location.key;
    previousKey.current = location.key;
    // Opening the browser list should not summon the mobile search keyboard.
    if (!navigating && !showDetail) return;
    if (!window.matchMedia?.("(max-width: 720px)").matches) return;
    const sidebar = root.current?.querySelector<HTMLElement>("[data-browser-list]");
    const detail = root.current?.querySelector<HTMLElement>("[data-browser-detail]");
    const target = showDetail
      ? detail?.querySelector<HTMLElement>("h2")
      : origin.current?.isConnected
        ? origin.current
        : sidebar?.querySelector<HTMLElement>('[aria-current="true"]') ?? sidebar?.querySelector<HTMLElement>("input");
    if (!target) return;
    if (showDetail) target.tabIndex = -1;
    target.focus({ preventScroll: true });
    (showDetail ? detail : sidebar)?.scrollIntoView({ block: "start" });
  }, [location.key, showDetail]);

  function rememberOrigin(event: React.MouseEvent<HTMLDivElement>) {
    const target = (event.target as HTMLElement).closest<HTMLElement>("button");
    if (target?.closest("[data-browser-list]") && target.hasAttribute("aria-current")) {
      origin.current = target;
    }
  }

  return { ref: root, onClickCapture: rememberOrigin };
}
