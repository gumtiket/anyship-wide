import { useEffect, useId, useMemo, useState } from 'react';
import { ChevronDown, FileCode2 } from 'lucide-react';
import { parseUnifiedDiff, type FileDiff } from './unifiedDiff';
import type { HighlightedDiff } from './diffSyntax';
import './file-diff.css';

const statuses = { added: '추가', deleted: '삭제', modified: '수정' };

export function FileDiffViewer({ diff }: { diff: string }) {
  const id = useId();
  const files = useMemo(() => parseUnifiedDiff(diff), [diff]);
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  const [highlighted, setHighlighted] = useState<{
    files: FileDiff[];
    tokens: Array<HighlightedDiff | null>;
  }>();
  useEffect(() => {
    if (!files) return;
    let active = true;
    // 읽기 쉬운 일반 diff를 먼저 보여 주고, 언어 문법은 검토할 때만 불러온다.
    void import('./diffSyntax')
      .then(({ highlightDiff }) => {
        if (active) setHighlighted({ files, tokens: files.map(highlightDiff) });
      })
      .catch(() => {
        /* 강조 모듈을 불러오지 못하면 일반 텍스트로 둔다. */
      });
    return () => {
      active = false;
    };
  }, [files]);
  if (!files)
    return (
      <div className="file-diff-fallback">
        <p className="small quiet">파일별 표시를 할 수 없어 원본 diff를 표시합니다.</p>
        <pre className="change-diff" aria-label="전체 변경안 원본">
          {diff}
        </pre>
      </div>
    );

  const additions = files.reduce((total, file) => total + file.additions, 0);
  const deletions = files.reduce((total, file) => total + file.deletions, 0);
  function toggle(index: number) {
    setCollapsed((previous) => {
      const next = new Set(previous);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }

  return (
    <div className="file-diffs" aria-label="파일별 코드 변경 내용">
      <div className="diff-toolbar">
        <div>
          <strong>변경 파일 {files.length}개</strong>
          <span className="diff-added" aria-label={`추가 ${additions}줄`}>
            +{additions}
          </span>
          <span className="diff-deleted" aria-label={`삭제 ${deletions}줄`}>
            −{deletions}
          </span>
        </div>
        <div>
          <button type="button" className="text-link" onClick={() => setCollapsed(new Set())}>
            모두 펼치기
          </button>
          <button
            type="button"
            className="text-link"
            onClick={() => setCollapsed(new Set(files.map((_, index) => index)))}
          >
            모두 접기
          </button>
        </div>
      </div>
      {files.map((file, index) => (
        <section
          className="diff-file"
          key={`${index}:${file.path}`}
          aria-label={`${file.path} 변경 내용`}
        >
          <button
            type="button"
            className="diff-file-header"
            aria-expanded={!collapsed.has(index)}
            aria-controls={`${id}-${index}`}
            onClick={() => toggle(index)}
          >
            <ChevronDown size={16} className={collapsed.has(index) ? 'is-collapsed' : ''} />
            <FileCode2 size={16} />
            <span className="diff-file-path">{file.path}</span>
            <span className="diff-file-status">{statuses[file.status]}</span>
            <span className="diff-file-stats">
              <span className="diff-added" aria-label={`추가 ${file.additions}줄`}>
                +{file.additions}
              </span>
              <span className="diff-deleted" aria-label={`삭제 ${file.deletions}줄`}>
                −{file.deletions}
              </span>
            </span>
          </button>
          <div
            id={`${id}-${index}`}
            className="diff-scroll"
            hidden={collapsed.has(index)}
            role="region"
            tabIndex={0}
            aria-label={`${file.path} diff, 가로 스크롤 가능`}
          >
            {!collapsed.has(index) && (
              <table className="diff-table" aria-label={`${file.path} 변경 전후 줄 비교`}>
                <thead className="diff-sr-only">
                  <tr>
                    <th scope="col">이전 줄</th>
                    <th scope="col">이후 줄</th>
                    <th scope="col">변경</th>
                    <th scope="col">코드</th>
                  </tr>
                </thead>
                <tbody>
                  {file.lines.map((line, lineIndex) => {
                    const tokens =
                      highlighted?.files === files
                        ? highlighted.tokens[index]?.[lineIndex]
                        : undefined;
                    return (
                      <tr key={lineIndex} className={`diff-line diff-${line.kind}`}>
                        <td className="diff-line-number">{line.oldNumber}</td>
                        <td className="diff-line-number">{line.newNumber}</td>
                        <td className="diff-line-sign">
                          {line.kind === 'addition' ? '+' : line.kind === 'deletion' ? '−' : ''}
                        </td>
                        <td className="diff-line-code">
                          <code>
                            {tokens?.length
                              ? tokens.map((token, tokenIndex) => (
                                  <span key={tokenIndex} className={token.className || undefined}>
                                    {token.text}
                                  </span>
                                ))
                              : line.text}
                          </code>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        </section>
      ))}
    </div>
  );
}
