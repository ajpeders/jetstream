import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [svelte()],
  build: {
    outDir: "static/build",
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      input: {
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
