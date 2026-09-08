import type { ComponentType } from 'react';
import { createRoot } from 'react-dom/client';

/**
 * Mount one React root per island present in the page.
 *
 * Each page entry hands in `[selector, Component]` pairs keyed on the custom
 * element tags and `[jet-*]` attributes the HTML shells already carry. A
 * shell that lacks a mount point simply gets no island — the bundle is loaded
 * by pages it does not own and must no-op quietly. One root per island (not
 * one per page) is deliberate: a panel can be ported or reverted alone, and
 * the page keeps working if one is absent from the markup.
 */
export function mountIslands(islands: ReadonlyArray<readonly [string, ComponentType]>): void {
  for (const [selector, Component] of islands) {
    for (const el of document.querySelectorAll(selector)) {
      createRoot(el).render(<Component />);
    }
  }
}
