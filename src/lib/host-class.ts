import { useLayoutEffect } from 'react';

/**
 * Keep a class on the island's host element in step with a boolean. The panels
 * render *into* the page's existing <section id="…-panel">, whose chrome in
 * the page CSS keys off `.collapsed` on the section itself. React never owns
 * that element, so the class is set imperatively. Layout effect so the class
 * lands in the same frame as the content change — no flash of expanded chrome.
 */
export function useHostClass(host: HTMLElement, className: string, on: boolean): void {
  useLayoutEffect(() => {
    host.classList.toggle(className, on);
  }, [host, className, on]);
}
