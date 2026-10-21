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
// An exported publication (db_cli export --out DIR) replaces the example fixtures when set.
const publicationDir = process.env.VITE_PUBLICATION_DIR;
// Where the site is served from: /meal-prep/ (this repository's Pages) or /meal-plan/ (the publication repository).
const basePath = process.env.VITE_BASE_PATH || '/meal-prep/';
if (!/^\/([A-Za-z0-9._~-]+\/)*$/.test(basePath)) throw new Error('VITE_BASE_PATH must start and end with "/" (for example /meal-plan/).');

function publicationPlugin(mode) {
  const fixtureDir = publicationDir ? path.resolve(repoDir, publicationDir) : path.join(repoDir, 'docs/migration-to-cloud/static-examples', fixtureName);
  if (mode === 'pages' && !publicationDir && !allowedFixtures.has(fixtureName)) throw new Error('VITE_STATIC_BUNDLE must be populated or empty.');
  if (mode === 'pages' && !fs.existsSync(path.join(fixtureDir, 'index.json'))) throw new Error('Selected static publication is missing index.json.');
  return {
    name: 'meal-prep-publication',
    configureServer(server) {
      if (mode !== 'pages') return;
      server.middlewares.use((req, res, next) => {
        const prefix = `${basePath}publication/`;
        if (!req.url?.startsWith(prefix)) return next();
        const relative = decodeURIComponent(req.url.slice(prefix.length).split('?')[0]);
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
    base: basePath, publicDir: false,
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
