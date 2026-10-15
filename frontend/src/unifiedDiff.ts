export type DiffLine = {
  kind: 'context' | 'addition' | 'deletion' | 'hunk' | 'meta';
  text: string;
  oldNumber?: number;
  newNumber?: number;
};

export type FileDiff = {
  path: string;
  status: 'added' | 'deleted' | 'modified';
  additions: number;
  deletions: number;
  lines: DiffLine[];
};

// 서비스의 difflib 형식과 Git 형식 unified 패치를 받는다. 지원하지 않거나 불완전한
// 패치는 null을 돌려주어 화면이 원본 diff 전체를 그대로 보여 주게 한다.
export function parseUnifiedDiff(diff: string): FileDiff[] | null {
  const source = diff.replace(/\r\n/g, '\n').split('\n');
  if (source.at(-1) === '') source.pop();
  const files: FileDiff[] = [];
  let current: FileDiff | undefined;
  let oldNumber = 0;
  let newNumber = 0;
  let oldRemaining = 0;
  let newRemaining = 0;
  let hasHunk = false;
  let pendingMetadata = false;
  const path = (header: string) =>
    header
      .slice(4)
      .split('\t')[0]
      .replace(/^[ab]\//, '');

  for (let index = 0; index < source.length; index++) {
    const line = source[index];
    if (line === '\\ No newline at end of file' && current && hasHunk && !pendingMetadata) {
      current.lines.push({ kind: 'meta', text: line });
      continue;
    }
    // 소스 줄 자체가 "--- " / "+++ " 파일 머리글처럼 보일 수 있다.
    // 다음 파일을 찾기 전에 선언된 hunk를 끝까지 읽는다.
    if (oldRemaining > 0 || newRemaining > 0) {
      if (!current) return null;
      if (line.startsWith(' ') && oldRemaining > 0 && newRemaining > 0) {
        current.lines.push({
          kind: 'context',
          text: line.slice(1),
          oldNumber: oldNumber++,
          newNumber: newNumber++,
        });
        oldRemaining--;
        newRemaining--;
      } else if (line.startsWith('-') && oldRemaining > 0) {
        current.lines.push({ kind: 'deletion', text: line.slice(1), oldNumber: oldNumber++ });
        current.deletions++;
        oldRemaining--;
      } else if (line.startsWith('+') && newRemaining > 0) {
        current.lines.push({ kind: 'addition', text: line.slice(1), newNumber: newNumber++ });
        current.additions++;
        newRemaining--;
      } else return null;
      continue;
    }
    if (line.startsWith('--- ') && source[index + 1]?.startsWith('+++ ')) {
      if (current && !hasHunk) return null;
      const oldPath = path(line);
      const newPath = path(source[++index]);
      // 따옴표로 감싼 Git 경로는 따로 해석해야 하므로 원본을 그대로 보여 준다.
      if (!oldPath || !newPath || oldPath.startsWith('"') || newPath.startsWith('"')) return null;
      current = {
        path: newPath === '/dev/null' ? oldPath : newPath,
        status:
          oldPath === '/dev/null' ? 'added' : newPath === '/dev/null' ? 'deleted' : 'modified',
        additions: 0,
        deletions: 0,
        lines: [],
      };
      files.push(current);
      hasHunk = false;
      pendingMetadata = false;
      continue;
    }
    const hunk = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*$/.exec(line);
    if (hunk && current) {
      if (pendingMetadata) return null;
      oldNumber = Number(hunk[1]);
      oldRemaining = Number(hunk[2] ?? 1);
      newNumber = Number(hunk[3]);
      newRemaining = Number(hunk[4] ?? 1);
      current.lines.push({ kind: 'hunk', text: line });
      hasHunk = true;
      continue;
    }
    if (
      line.startsWith('diff --git ') ||
      /^(index |new file mode |deleted file mode )/.test(line)
    ) {
      pendingMetadata = true;
      continue;
    }
    return null;
  }
  return files.length && hasHunk && !pendingMetadata && !oldRemaining && !newRemaining
    ? files
    : null;
}
