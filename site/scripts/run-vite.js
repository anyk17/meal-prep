import { spawnSync } from 'node:child_process';
import process from 'node:process';

const [, , command, mode] = process.argv;
if (!['dev', 'build', 'preview'].includes(command)) throw new Error('Use dev, build, or preview.');
if (!['api', 'pages'].includes(mode)) throw new Error(command === 'dev' ? 'Choose an explicit mode: npm run dev:api or npm run dev:static' : `Choose an explicit mode: npm run ${command}:api or npm run ${command}:pages`);
if (command === 'preview' && mode !== 'pages') throw new Error('Use npm run preview:pages.');
const hostArgs = command === 'build' ? [] : ['--host', '127.0.0.1'];
const forwardedArgs = process.argv.slice(4);
if (forwardedArgs.some(argument => argument === '--host' || argument === '-h' || argument.startsWith('--host='))) throw new Error('The dev server is restricted to 127.0.0.1. Forward other Vite options as needed.');
const result = spawnSync('vite', [command, '--mode', mode, ...forwardedArgs, ...hostArgs], { stdio: 'inherit', shell: process.platform === 'win32' });
process.exit(result.status ?? 1);
