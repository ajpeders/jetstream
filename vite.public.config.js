import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

/* The one public bundle. home.html is the 401 body, so this must be served
 * to anonymous visitors — app.py exempts /build-public/ from the gate. It is
 * a separate build with no code splitting so it never references a chunk
 * under the gated /build/ path. src/public/main.ts carries the content rule. */
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: {
    outDir: "static/build-public",
    emptyOutDir: true,
    rollupOptions: {
      input: { main: "src/public/main.tsx" },
      output: { entryFileNames: "[name].js", inlineDynamicImports: true }
    }
  }
});
