import assert from 'node:assert/strict';
import { test } from 'node:test';
import { diffLanguage, highlightDiff } from '../src/diffSyntax.ts';
import { parseUnifiedDiff, type FileDiff } from '../src/unifiedDiff.ts';

function added(path: string, text: string): FileDiff {
  const lines = text.split('\n');
  return {
    path,
    status: 'added',
    additions: lines.length,
    deletions: 0,
    lines: lines.map((text, index) => ({ kind: 'addition', text, newNumber: index + 1 })),
  };
}

test('selects languages from paths and known filenames without guessing unknown files', () => {
  for (const [path, language] of Object.entries({
    'app.py': 'python',
    'SRC/A.TSX': 'typescript',
    'app.jsx': 'javascript',
    'app.mjs': 'javascript',
    'a.cts': 'typescript',
    'config.json': 'json',
    'compose.yml': 'yaml',
    'page.html': 'xml',
    'app.css': 'css',
    'run.sh': 'bash',
    Dockerfile: 'dockerfile',
    'build/Dockerfile.prod': 'dockerfile',
    '.env.local': 'bash',
    'pyproject.toml': 'ini',
  })) {
    assert.equal(diffLanguage(path), language, path);
  }
  for (const path of ['README', 'notes.txt', 'code.unknown', 'constructor', 'file.constructor'])
    assert.equal(diffLanguage(path), undefined);
});

test('highlights supported languages while preserving exact text and whitespace', () => {
  const samples = {
    'app.py': 'def greet(name):\n\t# 한글 주석\n\treturn "hello " + name  \n',
    'app.js': 'export function greet() { return "hello"; }',
    'app.ts': 'const count: number = 42;',
    'app.jsx': 'export const App = () => <div title="hello">Hello</div>;',
    'app.tsx': 'export const App = (props: {name: string}) => <div>{props.name}</div>;',
    'a.json': '{"name": "demo", "enabled": true}',
    'a.yml': 'services:\n  web:\n    image: "demo"',
    'a.html': '<script>alert("hello")</script>\n<img src=x onerror="alert(1)"> &lt; & >',
    'a.css': '.app { color: red; }',
    'run.sh': '#!/bin/sh\necho "hello"',
    Dockerfile: 'FROM python:3.12\nRUN echo "hello"',
    'a.toml': '[project]\nname = "demo"',
  };
  for (const [path, source] of Object.entries(samples)) {
    const file = added(path, source);
    const before = structuredClone(file);
    const result = highlightDiff(file);
    assert.ok(result, path);
    assert.deepEqual(
      result.map((tokens) => tokens!.map((token) => token.text).join('')),
      source.split('\n'),
      path,
    );
    assert.ok(
      result.some((tokens) => tokens!.some((token) => token.className)),
      path,
    );
    assert.deepEqual(file, before, 'highlighting must not mutate the diff');
  }
});

test('keeps multiline comments and strings within each hunk', () => {
  const result = highlightDiff(added('app.py', 'text = """start\ninside\nend"""\n# comment'))!;
  assert.ok(result[1]!.every((token) => token.className.includes('hljs-string')));
  assert.ok(result[3]!.some((token) => token.className.includes('hljs-comment')));
  const js = highlightDiff(added('app.js', '/* start\ninside\nend */\nconst n = 2;'))!;
  assert.ok(js[1]!.every((token) => token.className.includes('hljs-comment')));
  assert.ok(js[3]!.some((token) => token.className.includes('hljs-keyword')));
});

test('keeps old and new lexical states separate and resets at hunk gaps', () => {
  const files = parseUnifiedDiff(
    '--- a/app.js\n+++ b/app.js\n@@ -1,3 +1,2 @@\n-/* old comment\n+const n = 1;\n shared\n-*/\n@@ -50 +49 @@\n-let old = 1;\n+const fresh = 2;\n',
  );
  assert.ok(files);
  const result = highlightDiff(files[0])!;
  assert.ok(result[1]!.some((token) => token.className.includes('hljs-comment')));
  assert.ok(
    result[2]!.some((token) => token.text === 'const' && token.className.includes('hljs-keyword')),
  );
  assert.ok(
    result[3]!.every((token) => !token.className.includes('hljs-comment')),
    'context uses the new version',
  );
  assert.equal(result[0], undefined);
  assert.equal(result[5], undefined);
  assert.ok(
    result[7]!.some((token) => token.text === 'const' && token.className.includes('hljs-keyword')),
  );

  const gaps = parseUnifiedDiff(
    '--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+"""open\n@@ -50 +50 @@\n-old\n+def fresh(): pass\n',
  )!;
  assert.ok(
    highlightDiff(gaps[0])![5]!.some(
      (token) => token.text === 'def' && token.className.includes('hljs-keyword'),
    ),
  );
});

test('preserves no-newline markers and handles deleted files and blank code lines', () => {
  const file = parseUnifiedDiff(
    '--- a/old.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-print("old")\n-\n\\ No newline at end of file\n',
  )![0];
  const result = highlightDiff(file)!;
  assert.equal(result[0], undefined);
  assert.ok(result[1]!.some((token) => token.className));
  assert.deepEqual(result[2], []);
  assert.equal(result[3], undefined);
});

test('falls back for unknown languages and oversized or minified diffs', () => {
  assert.equal(highlightDiff(added('notes.txt', 'const n = 1;')), null);
  assert.equal(highlightDiff(added('large.py', 'x\n'.repeat(1000))), null);
  assert.equal(highlightDiff(added('large.py', 'x'.repeat(2001))), null);
  assert.equal(highlightDiff(added('large.py', ('x'.repeat(1000) + '\n').repeat(51))), null);
});
