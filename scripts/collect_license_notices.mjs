#!/usr/bin/env node
/** Preserve installed npm notice documents and the pinned Node image's complete license. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { parseArgs } from 'node:util';

const { values } = parseArgs({ options: {
  'node-modules': { type: 'string' },
  'node-license': { type: 'string' },
  output: { type: 'string' },
  supplements: { type: 'string' },
} });
for (const key of ['node-modules', 'node-license', 'output']) {
  if (!values[key]) throw new Error(`Required argument: --${key}`);
}
const modules = path.resolve(values['node-modules']);
const output = path.resolve(values.output);
const supplements = values.supplements ? JSON.parse(fs.readFileSync(values.supplements, 'utf8')) : {};
if (fs.existsSync(output) && fs.readdirSync(output).length) {
  throw new Error('Output must be absent or empty');
}
const manifest = {
  format: 1,
  scope: 'Conservative notices from every installed dashboard npm package, including build-only packages; this is not a list of shipped code or a legal certification.',
  node_version: process.version,
  packages: [],
  files: {},
};
const noticeName = /^(licen[sc]e|copying|notice|copyright|authors|third[._-]?party[._-]?(?:notices?|licen[sc]es?))(?:$|[._-])/i;
const codeExtension = /\.(?:[cm]?js|[cm]?ts|jsx|tsx|py|sh|c|h|cpp|go|rs|map|wasm|node)$/i;
const entries = directory => fs.readdirSync(directory, { withFileTypes: true }).sort((a,b) => a.name.localeCompare(b.name, 'en'));

function preserve(source, destination) {
  const bytes = fs.readFileSync(source);
  new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  if (bytes.includes(0)) throw new Error(`Notice is not a text document: ${source}`);
  const target = path.join(output, destination);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, bytes);
  manifest.files[destination] = crypto.createHash('sha256').update(bytes).digest('hex');
}

function noticeFiles(directory, prefix = '') {
  const found = [];
  for (const item of entries(directory)) {
    if (item.name === 'node_modules' || item.name === '.git') continue;
    const relative = prefix + item.name;
    const full = path.join(directory, item.name);
    if (item.isDirectory()) {
      found.push(...noticeFiles(full, relative + '/'));
    } else if (item.isFile() && noticeName.test(item.name) && !codeExtension.test(item.name)) {
      found.push(relative);
    }
  }
  return found;
}

function packageAt(directory) {
  const metadataFile = path.join(directory, 'package.json');
  if (!fs.existsSync(metadataFile)) return;
  const metadata = JSON.parse(fs.readFileSync(metadataFile, 'utf8'));
  if (!metadata.name || !metadata.version) throw new Error(`Missing package identity: ${metadataFile}`);
  const installedPath = path.relative(modules, directory).split(path.sep).join('/');
  let documents = noticeFiles(directory);
  let readmeFallback = false;
  if (!documents.length) {
    documents = entries(directory).filter(item => item.isFile() && /^readme(?:$|\.)/i.test(item.name)).map(item => item.name);
    readmeFallback = true;
  }
  const retained = [];
  const supplementalNotices = [];
  for (const document of documents.sort()) {
    const safeDocument = /\.(?:html?|xhtml|svg|xml)$/i.test(document) ? document + '.txt' : document;
    const destination = `packages/${installedPath}/${safeDocument}`;
    preserve(path.join(directory, document), destination);
    retained.push(destination);
  }
  for (const [index, supplement] of (supplements[`${metadata.name}@${metadata.version}`] ?? []).entries()) {
    const source = path.resolve(path.dirname(values.supplements), supplement.file);
    const bytes = fs.readFileSync(source);
    const actual = crypto.createHash('sha256').update(bytes).digest('hex');
    if (actual !== supplement.sha256) throw new Error(`Supplement checksum mismatch: ${source}`);
    const destination = `packages/${installedPath}/SUPPLEMENT-${index}-${path.basename(supplement.file)}.txt`;
    preserve(source, destination);
    retained.push(destination);
    supplementalNotices.push({ source: supplement.source, document: destination, sha256: actual, basis: supplement.basis ?? "Pinned source notice omitted from the published npm package" });
  }
  manifest.packages.push({
    name: metadata.name,
    version: metadata.version,
    installed_path: installedPath,
    declared_license: metadata.license ?? metadata.licenses ?? null,
    author: metadata.author ?? null,
    contributors: metadata.contributors ?? null,
    repository: metadata.repository ?? null,
    notice_documents: retained,
    supplemental_notices: supplementalNotices,
    readme_fallback: readmeFallback,
    notice_status: retained.length ? (readmeFallback && !supplementalNotices.length ? 'readme-only-review' : 'documents-retained') : 'no-document-review',
  });
  const nested = path.join(directory, 'node_modules');
  if (fs.existsSync(nested)) packagesIn(nested);
}

function packagesIn(directory) {
  for (const item of entries(directory)) {
    if (item.name.startsWith('.')) continue;
    const full = path.join(directory, item.name);
    if (item.isSymbolicLink()) throw new Error(`Linked package needs an explicit notice review: ${full}`);
    if (!item.isDirectory()) continue;
    if (item.name.startsWith('@')) {
      for (const child of entries(full)) {
        if (child.isSymbolicLink()) throw new Error(`Linked package needs an explicit notice review: ${child.name}`);
        if (child.isDirectory()) packageAt(path.join(full, child.name));
      }
    } else {
      packageAt(full);
    }
  }
}

preserve(path.resolve(values['node-license']), 'node/LICENSE');
packagesIn(modules);
if (!manifest.packages.length) throw new Error('No installed npm packages found');
manifest.packages.sort((a,b) => a.installed_path.localeCompare(b.installed_path, 'en'));
manifest.files = Object.fromEntries(Object.entries(manifest.files).sort(([a],[b]) => a.localeCompare(b, 'en')));
fs.writeFileSync(path.join(output, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
fs.writeFileSync(path.join(output, 'NOTICE.txt'),
  'This directory preserves Node and dashboard dependency notice documents.\n' +
  'Original document bytes are retained; manifest.json records package identity, source-relative paths, declared licenses and SHA-256 hashes.\n' +
  'Build-only package notices are included conservatively and do not imply those packages are shipped.\n' +
  'A package without a standalone notice retains its README where present and is marked for review.\n' +
  'Declared licenses and collected files do not certify license compliance.\n');
console.log(`Retained ${Object.keys(manifest.files).length} documents for Node and ${manifest.packages.length} installed npm packages`);
