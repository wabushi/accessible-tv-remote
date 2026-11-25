import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const iconPath = resolve(import.meta.dirname, '../Icon.png');

export default defineConfig({
  plugins: [
    react(),
    {
      name: 'serve-project-icon',
      async generateBundle() {
        this.emitFile({ type: 'asset', fileName: 'Icon.png', source: await readFile(iconPath) });
      },
    },
  ],
  build: { outDir: 'dist', emptyOutDir: true },
});
