import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import process from 'node:process';

const siteDir = path.dirname(fileURLToPath(import.meta.url));
const repoDir = path.resolve(siteDir, '..');
const fixtureName = process.env.VITE_STATIC_BUNDLE || 'populated';
const allowedFixtures = new Set(['populated', 'empty']);

function publicationPlugin(mode) {
  const fixtureDir = path.join(repoDir, 'docs/migration-to-cloud/static-examples', fixtureName);
  if (mode === 'pages' && !allowedFixtures.has(fixtureName)) throw new Error('VITE_STATIC_BUNDLE must be populated or empty.');
  if (mode === 'pages' && !fs.existsSync(path.join(fixtureDir, 'index.json'))) throw new Error('Selected static publication fixture is missing.');
  return {
    name: 'meal-prep-publication',
    configureServer(server) {
      if (mode !== 'pages') return;
      server.middlewares.use((req, res, next) => {
        const basePath = '/meal-prep/publication/';
        if (!req.url?.startsWith(basePath)) return next();
        const relative = decodeURIComponent(req.url.slice(basePath.length).split('?')[0]);
        const file = path.resolve(fixtureDir, relative);
        if (!file.startsWith(`${path.resolve(fixtureDir)}${path.sep}`) && file !== path.join(fixtureDir, 'index.json')) { res.statusCode = 400; return res.end(); }
        if (!fs.existsSync(file) || !fs.statSync(file).isFile()) { res.statusCode = 404; return res.end(); }
        res.setHeader('Content-Type', 'application/json'); res.setHeader('Cache-Control', 'no-store'); fs.createReadStream(file).pipe(res);
      });
    },
    generateBundle() {
      if (mode !== 'pages') return;
      const addFiles = directory => {
        for (const entry of fs.readdirSync(directory,{withFileTypes:true})) {
          const file = path.join(directory,entry.name);
          if (entry.isDirectory()) addFiles(file);
          else this.emitFile({type:'asset',fileName:`publication/${path.relative(fixtureDir,file).split(path.sep).join('/')}`,source:fs.readFileSync(file)});
        }
      };
      addFiles(fixtureDir);
    },
  };
}

export default defineConfig(({ mode }) => {
  if (!['api', 'pages'].includes(mode)) throw new Error('Build/dev mode is required. Run npm run dev:api, dev:static, build:api, or build:pages.');
  const isPages = mode === 'pages';
  return {
    base: '/meal-prep/', publicDir: false,
    plugins: [react(), publicationPlugin(mode)],
    server: { host: '127.0.0.1', proxy: isPages ? undefined : { '/api': { target: 'http://127.0.0.1:5000', changeOrigin: false } } },
    define: {
      'import.meta.env.VITE_READER_MODE': JSON.stringify(isPages ? 'static' : 'api'),
      'import.meta.env.VITE_API_BASE_PATH': JSON.stringify('/api/v1'),
      'import.meta.env.VITE_DEFAULT_FROM': JSON.stringify(process.env.VITE_DEFAULT_FROM || '2026-10-19'),
      'import.meta.env.VITE_DEFAULT_TO': JSON.stringify(process.env.VITE_DEFAULT_TO || '2026-10-25'),
    },
  };
});
