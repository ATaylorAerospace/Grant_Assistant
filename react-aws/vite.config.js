import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Vite replaces Create React App (react-scripts). Builds take seconds instead of
// minutes, which is what makes scripts/deploy-ui.sh a ~2-minute UI-only deploy.
//
// - `build.outDir` stays `build/` so the post-deployment seeder and
//   scripts/deploy-ui.sh zip the same directory CRA produced.
// - Env vars exposed to the browser must be prefixed VITE_ (was REACT_APP_).
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'build',
    sourcemap: false,
  },
  server: {
    port: 3000,
    open: false,
  },
  define: {
    // A few transitive deps still probe `global`; map it to the browser window.
    global: 'window',
  },
});
