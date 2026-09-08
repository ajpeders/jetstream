import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

/* The gated bundles: everything under static/build/ is served behind the
 * token gate (see _gate_viewer_routes in app.py). React and the shared lib
 * land in chunks/ and are shared by the admin and viewer entries.
 *
 * The public bundle is a separate config because it must not depend on a
 * chunk under this gated path. */
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: {
    outDir: "static/build",
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      input: {
        admin: "src/admin/main.tsx",
        hub: "src/hub/main.tsx",
        viewer: "src/viewer/main.tsx"
      },
      output: {
        entryFileNames: "[name].js",
        chunkFileNames: "chunks/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]"
      }
    }
  }
});
