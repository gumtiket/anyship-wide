import { useEffect, useState } from 'react';
import {
  Check,
  ExternalLink,
  FileCode2,
  GitPullRequest,
  LoaderCircle,
  RefreshCw,
  Sparkles,
} from 'lucide-react';
import { AnalysisWorkflow } from './AnalysisWorkflow';
import { FileDiffViewer } from './FileDiffViewer';

type Change = {
  id: string;
  status: string;
  file: string;
  title: string;
  content: string;
  diff: string;
  review_hash: string;
  base_sha: string;
  branch: string;
  commit_sha: string;
  pr_url: string;
  pr_number: number;
  busy: boolean;
  error: string;
};
type Props = {
  projectId: string;
  csrf: string;
  aiMode: string;
  provider?: string;
  request: <T>(path: string, options?: RequestInit) => Promise<T>;
  onError: (error: unknown) => void;
};

export function ChangeWorkflow(props: Props) {
  return props.aiMode === 'bronze' ? (
    <AnalysisWorkflow {...props} provider={props.provider ?? 'none'} />
  ) : (
    <PlaceholderWorkflow {...props} />
  );
}

function PlaceholderWorkflow({ projectId, csrf, aiMode, request, onError }: Props) {
  const [change, setChange] = useState<Change | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [error, setError] = useState('');
  const endpoint = `/projects/${projectId}`;
  useEffect(() => {
    let active = true;
    request<Change | null>(endpoint + '/changes')
      .then((value) => {
        if (active) setChange(value);
      })
      .catch((e) => {
        if (active) {
          setError(e.message);
          onError(e);
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [endpoint]);
  useEffect(() => {
    if (!change?.busy) return;
    let active = true;
    const timer = setInterval(() => {
      request<Change>(endpoint + '/changes')
        .then((value) => {
          if (active) setChange(value);
        })
        .catch((e) => {
          if (active) {
            setError(e.message);
            onError(e);
          }
        });
    }, 3000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [endpoint, change?.busy]);
  async function action(suffix: string, body: unknown = {}) {
    setBusy(true);
    setError('');
    try {
      setChange(
        await request<Change>(endpoint + suffix, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
          body: JSON.stringify(body),
        }),
      );
      setReviewed(false);
      setSelected(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : '작업을 처리하지 못했습니다.');
      onError(e);
      try {
        setChange(await request<Change | null>(endpoint + '/changes'));
      } catch {
        /* 작업 오류를 그대로 둔다. */
      }
    } finally {
      setBusy(false);
    }
  }
  const waiting = busy || Boolean(change?.busy);
  const prepared = Boolean(change?.diff);
  const completed = change?.status === 'pr_created';
  const enabled = aiMode === 'placeholder';
  return (
    <section className="panel change-workflow" aria-label="수정 및 PR 작업">
      <div className="panel-heading">
        <h2>
          <Sparkles size={18} /> 분석 · 수정 · PR
        </h2>
        <span className="status">{enabled ? 'AI만 임시 모드' : 'AI 연결 대기'}</span>
      </div>
      <p className="workflow-description">
        {enabled
          ? 'AI 대신 미연결 안내 주석을 추가하는 수정 항목을 반환합니다. 검토 후 PR을 생성하면 연결한 실제 GitHub 저장소에 새 브랜치와 커밋이 만들어집니다.'
          : 'AI 분석 모듈이 아직 연결되지 않았습니다. 관리자에게 AI 연결 또는 개발용 임시 모드 설정을 요청해 주세요.'}
      </p>
      <ol className="workflow-stages" aria-label="진행 단계">
        <li className={change ? 'done' : 'current'}>1 수정 항목 등록</li>
        <li className={prepared ? 'done' : change ? 'current' : ''}>2 수정 · 검토</li>
        <li className={completed ? 'done' : prepared ? 'current' : ''}>3 GitHub PR 생성</li>
      </ol>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      {change?.error && change.error !== error && (
        <p className="error" role="alert">
          {change.error}
        </p>
      )}
      <div aria-live="polite">
        {(loading || waiting) && (
          <p className="workflow-busy">
            <LoaderCircle size={16} className="spin" />{' '}
            {loading
              ? '작업을 불러오는 중입니다.'
              : '요청을 처리하고 있습니다. 새로고침해도 진행 기록은 유지됩니다.'}
          </p>
        )}
      </div>
      {!loading && !change && (
        <button
          className="primary"
          disabled={waiting || !enabled}
          onClick={() => action('/analysis')}
        >
          <Sparkles size={16} /> 분석 요청 · 임시 모드
        </button>
      )}
      {change && !prepared && (
        <div className="change-item">
          <label className="review-check">
            <input
              type="checkbox"
              checked={selected}
              onChange={(e) => setSelected(e.target.checked)}
              disabled={waiting || !enabled}
            />
            <span>
              <strong>AI 분석 모듈 미연결 안내 파일 추가</strong>
              <small>{change.file} · 안내 주석만 포함</small>
            </span>
          </label>
          <pre>{change.content}</pre>
          <button
            className="primary"
            disabled={waiting || !selected || !enabled}
            onClick={() => action('/changes/apply')}
          >
            <FileCode2 size={16} /> 선택한 수정 실행
          </button>
        </div>
      )}
      {change && prepared && (
        <>
          <div className="workflow-verification">
            <Check size={15} /> 변경 내용 저장 · Python 문법 확인 완료
            <span>프로젝트 빌드·테스트는 실행하지 않았습니다.</span>
          </div>
          <p className="small quiet">
            기준 커밋 <code>{change.base_sha.slice(0, 12)}</code> · 작업 브랜치{' '}
            <code>{change.branch}</code>
          </p>
          <FileDiffViewer key={`${change.id}:${change.review_hash}`} diff={change.diff} />
          {!completed && (
            <>
              <label className="review-check">
                <input
                  type="checkbox"
                  checked={reviewed}
                  onChange={(e) => setReviewed(e.target.checked)}
                  disabled={waiting || !enabled}
                />
                <span>
                  변경 내용을 확인했습니다. 실제 GitHub 저장소에 브랜치·커밋과 Draft PR을
                  생성합니다.
                </span>
              </label>
              <button
                className="primary"
                disabled={waiting || !reviewed || !enabled}
                onClick={() => action('/changes/pr', { review_hash: change.review_hash })}
              >
                <GitPullRequest size={16} /> GitHub Draft PR 생성
              </button>
            </>
          )}
        </>
      )}
      {change && completed && (
        <div className="published-pr" role="status">
          <div>
            <GitPullRequest size={20} />
            <h3>GitHub PR #{change.pr_number} 생성 완료</h3>
          </div>
          <p>{change.title}</p>
          <p className="small">
            커밋 <code>{change.commit_sha.slice(0, 12)}</code> · Draft PR로 생성했습니다.
          </p>
          <a className="primary" href={change.pr_url} target="_blank" rel="noreferrer">
            GitHub에서 PR 보기 <ExternalLink size={16} />
          </a>
        </div>
      )}
      {change && !completed && (
        <div className="workflow-reset">
          <button
            className="text-link"
            disabled={waiting || !enabled}
            onClick={() => action('/analysis', { restart: true })}
          >
            <RefreshCw size={14} /> 최신 코드로 다시 요청
          </button>
          <p className="small quiet">
            현재 검토를 초기화합니다. 이전 시도에서 생성한 GitHub 브랜치는 자동 삭제하지 않습니다.
          </p>
        </div>
      )}
    </section>
  );
}
