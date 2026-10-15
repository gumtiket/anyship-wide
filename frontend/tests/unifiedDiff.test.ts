import assert from 'node:assert/strict';
import { test } from 'node:test';
import { parseUnifiedDiff } from '../src/unifiedDiff.ts';

test('splits difflib output into modified, added and deleted files with line numbers', () => {
  const files = parseUnifiedDiff(
    '--- a/app.py\n+++ b/app.py\n@@ -5,2 +5,2 @@\n keep\n-old\n+new\n--- /dev/null\n+++ b/설정 파일.env\n@@ -0,0 +1 @@\n+PORT=8000\n--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-removed\n',
  );
  assert.ok(files);
  assert.deepEqual(
    files.map(({ path, status, additions, deletions }) => ({ path, status, additions, deletions })),
    [
      { path: 'app.py', status: 'modified', additions: 1, deletions: 1 },
      { path: '설정 파일.env', status: 'added', additions: 1, deletions: 0 },
      { path: 'old.py', status: 'deleted', additions: 0, deletions: 1 },
    ],
  );
  assert.deepEqual(files[0].lines.slice(1), [
    { kind: 'context', text: 'keep', oldNumber: 5, newNumber: 5 },
    { kind: 'deletion', text: 'old', oldNumber: 6 },
    { kind: 'addition', text: 'new', newNumber: 6 },
  ]);
});

test('handles Git headers, multiple hunks, CRLF and missing terminal newlines', () => {
  const files = parseUnifiedDiff(
    'diff --git a/app.py b/app.py\r\nindex 123..456 100644\r\n--- a/app.py\r\n+++ b/app.py\r\n@@ -1 +1 @@\r\n-a\r\n+b\r\n@@ -20 +22 @@ section\r\n-c\r\n\\ No newline at end of file\r\n+d\r\n\\ No newline at end of file\r\n',
  );
  assert.ok(files);
  assert.equal(files.length, 1);
  assert.equal(files[0].additions, 2);
  assert.equal(files[0].deletions, 2);
  assert.deepEqual(files[0].lines[6], { kind: 'addition', text: 'd', newNumber: 22 });
  assert.equal(files[0].lines[7].kind, 'meta');
});

test('does not mistake source lines resembling file headers for another file', () => {
  const files = parseUnifiedDiff(
    '--- a/text.txt\n+++ b/text.txt\n@@ -1 +1 @@\n--- old heading\n+++ new heading\n',
  );
  assert.ok(files);
  assert.equal(files.length, 1);
  assert.equal(files[0].lines[1].text, '-- old heading');
  assert.equal(files[0].lines[2].text, '++ new heading');
});

test('falls back for empty, truncated, unexpected or unsupported patch data', () => {
  for (const diff of [
    '',
    'unexpected text',
    '--- a/a\n+++ b/a\n',
    '--- a/a\n+++ b/a\n@@ -1,2 +1 @@\n-a\n+b\n',
    '--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\nunrecognized\n',
    'Binary files a/a and b/a differ',
    'diff --git a/a b/a\nold mode 100644\nnew mode 100755\n',
  ]) {
    assert.equal(parseUnifiedDiff(diff), null, diff);
  }
});

test('supports the placeholder new-file patch and blank added lines', () => {
  const files = parseUnifiedDiff(
    'diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+# comment\n+\n',
  );
  assert.ok(files);
  assert.equal(files[0].status, 'added');
  assert.equal(files[0].additions, 2);
  assert.equal(files[0].lines[2].newNumber, 2);
  assert.equal(files[0].lines[2].text, '');
});

test('does not silently hide an incomplete second Git file', () => {
  assert.equal(
    parseUnifiedDiff(
      '--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\ndiff --git a/other b/other\nnew file mode 100644\n',
    ),
    null,
  );
});

test('preserves code whitespace and text that must be displayed literally', () => {
  const files = parseUnifiedDiff(
    '--- a/template.html\n+++ b/template.html\n@@ -1 +1 @@\n-\t  old  \n+\t  <script>alert("diff")</script>  \n',
  );
  assert.ok(files);
  assert.equal(files[0].lines[1].text, '\t  old  ');
  assert.equal(files[0].lines[2].text, '\t  <script>alert("diff")</script>  ');
});

test('falls back instead of mislabelling Git-quoted file paths', () => {
  assert.equal(
    parseUnifiedDiff('--- "a/special\\tname"\n+++ "b/special\\tname"\n@@ -1 +1 @@\n-a\n+b\n'),
    null,
  );
});
