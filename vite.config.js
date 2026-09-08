import react from "@vitejs/plugin-react";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

/* The gated bundles: everything under static/build/ is served behind the
 * token gate (see _gate_viewer_routes in app.py). React and the shared lib
 * land in chunks/ and are shared by the admin and viewer entries.
 *
 * The Svelte entries and plugin are transitional — they go when the last
 * .svelte island is ported (see docs/superpowers/specs/2026-09-08-react-
 * migration-design.md). The public bundle is a separate config because it
 * must not depend on a chunk under this gated path. */
export default defineConfig({
  plugins: [react(), svelte()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: {
    outDir: "static/build",
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      input: {
        admin: "src/admin/main.tsx",
        viewer: "src/viewer/main.tsx",
        viewerChat: "src/viewer-chat.js",
        adminQueue: "src/admin-queue.js",
        adminRecent: "src/admin-recent.js"
      },
      output: {
        entryFileNames: "[name].js",
        chunkFileNames: "chunks/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]"
      }
    }
  }
});
