import { useEffect, useRef, useState } from 'react';
import { ExternalLink, GitPullRequest, LoaderCircle, Sparkles } from 'lucide-react';
import { mutation } from './api';
import { FileDiffViewer } from './FileDiffViewer';

type Finding = {
  id: string;
  factor: number;
  file: string;
  line: number;
  evidence: string;
  description?: string;
  rule?: string;
  change_class?: string;
};
type Warning = { code: string; message: string };
type Report = {
  status?: string;
  execution_source?: string;
  adapter_compatible?: boolean;
  source_skipped_files?: number;
  diagnosis?: {
    support_grade: string;
    enrichment_status: string;
    violations: Finding[];
    review_candidates: Finding[];
    warnings: Warning[];
    framework?: { reason: string };
  };
  transformation?: {
    status: string;
    addressed_ids: string[];
    deferred_ids: string[];
    needs_approval: boolean;
    patch_valid: boolean;
    compile_passed: boolean;
    warnings: Warning[];
  };
  recommendation?: {
    set: string | null;
    rationale: string | null;
    estimated_monthly_cost: number | null;
  };
  gate_report?: {
    status: string;
    reason: string;
    pr_eligible: boolean;
    historical_status: string | null;
  };
  cost?: {
    historical: boolean;
    external_calls: number | null;
    total: {
      cost_usd: number | null;
      known_cost_usd: number;
      usage_complete: boolean;
      pricing_complete: boolean;
    };
  };
  packaging_warnings?: Warning[];
};
type Run = {
  id: string;
  status: string;
  provider: string;
  target_env: string;
  base_sha: string;
  base_branch: string;
  created_at: number;
  publish_status: string;
  busy: boolean;
  error: string;
  report: Report;
  files: { path: string; content: string }[];
  diff: string;
  review_hash: string;
  logs: { stage: string; at: number }[];
  pr_url: string;
  pr_number: number;
};
type Props = {
  projectId: string;
  csrf: string;
  provider: string;
  request: <T>(path: string, options?: RequestInit) => Promise<T>;
  onError: (error: unknown) => void;
};
const statuses: Record<string, string> = {
  queued: '대기',
  running: '분석 중',
  completed: '처리 완료',
  failed: '실패',
  interrupted: '중단',
  diagnosed: '진단 완료',
  partial: '일부 제안',
  unsupported: '지원 범위 밖',
  supported: '지원',
  not_run: '미실행',
  skipped: '생략',
  passed: '통과',
};
const providers: Record<string, string> = {
  none: '규칙 분석 · 모델 미사용',
  fake: '개발용 모의 모델',
  bedrock: 'Amazon Bedrock',
  anthropic: 'Anthropic',
};
const label = (status: string | undefined) =>
  status ? (statuses[status] ?? status) : '확인되지 않음';
const message = (error: unknown) =>
  error instanceof Error ? error.message : '작업을 처리하지 못했습니다.';

export function AnalysisWorkflow({ projectId, csrf, provider, request, onError }: Props) {
  const endpoint = `/projects/${projectId}/analyses`;
  const [history, setHistory] = useState<Run[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [target, setTarget] = useState('aws');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [reviewed, setReviewed] = useState(false);
  const [risky, setRisky] = useState(false);
  const generation = useRef(0);
  // 네트워크 실패로 결과가 불확실하면 키를 유지해, 재시도가 분석을 중복 실행하지 않게 한다.
  const pending = useRef<{ request_id: string; target_env: string } | null>(null);

  useEffect(() => {
    let active = true;
    request<Run[]>(endpoint)
      .then(async (rows) => {
        if (!active) return;
        setHistory(rows);
        if (rows.length) {
          const selected = await request<Run>(`${endpoint}/${rows[0].id}`);
          if (active) setRun(selected);
        }
      })
      .catch((e) => {
        if (active) {
          setError(message(e));
          onError(e);
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      generation.current++;
    };
  }, [endpoint]);

  useEffect(() => {
    setReviewed(false);
    setRisky(false);
  }, [run?.id, run?.review_hash]);
  useEffect(() => {
    if (!run?.busy) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await request<Run>(`${endpoint}/${run.id}`);
        if (!active) return;
        setRun(next);
        setHistory((rows) => rows.map((row) => (row.id === next.id ? next : row)));
      } catch (e) {
        if (active) {
          setError(message(e));
          onError(e);
        }
      }
      if (active) timer = setTimeout(poll, 2000);
    };
    timer = setTimeout(poll, 1200);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [endpoint, run?.id, run?.busy]);

  async function select(id: string) {
    const version = ++generation.current;
    setBusy(true);
    setError('');
    try {
      const next = await request<Run>(`${endpoint}/${id}`);
      if (version === generation.current) setRun(next);
    } catch (e) {
      setError(message(e));
      onError(e);
    } finally {
      if (version === generation.current) setBusy(false);
    }
  }
  async function start() {
    setBusy(true);
    setError('');
    pending.current ??= { request_id: crypto.randomUUID(), target_env: target };
    try {
      const next = await request<Run>(endpoint, mutation(csrf, pending.current));
      pending.current = null;
      setRun(next);
      setHistory((rows) => [next, ...rows.filter((row) => row.id !== next.id)]);
    } catch (e) {
      setError(message(e));
      onError(e);
    } finally {
      setBusy(false);
    }
  }
  async function publish() {
    if (!run) return;
    setBusy(true);
    setError('');
    try {
      setRun(
        await request<Run>(
          `${endpoint}/${run.id}/pr`,
          mutation(csrf, { review_hash: run.review_hash, approve_risky: risky }),
        ),
      );
    } catch (e) {
      setError(message(e));
      onError(e);
      try {
        setRun(await request<Run>(`${endpoint}/${run.id}`));
      } catch {
        /* 게시 오류를 그대로 둔다. */
      }
    } finally {
      setBusy(false);
    }
  }
  const report = run?.report;
  const diagnosis = report?.diagnosis;
  const transform = report?.transformation;
  const gate = report?.gate_report;
  const cost = report?.cost;
  const waiting = busy || loading || Boolean(run?.busy);
  const activeAnalysis = history.some((row) => ['queued', 'running'].includes(row.status));
  const publishable =
    run?.status === 'completed' &&
    !!run.review_hash &&
    report?.status !== 'failed' &&
    transform?.status !== 'failed';
  const warnings = [
    ...(diagnosis?.warnings ?? []),
    ...(transform?.warnings ?? []),
    ...(report?.packaging_warnings ?? []),
  ];
  return (
    <section
      className="panel change-workflow analysis-workflow"
      aria-label="AI 분석 및 변경안 검토"
    >
      <div className="panel-heading">
        <h2>
          <Sparkles size={18} /> AI 분석 · 변경안 · Draft PR
        </h2>
        <span className="status">{providers[provider] ?? provider}</span>
      </div>
      <p>
        기준 커밋의 소스를 분석해 진단과 변경안을 함께 생성합니다. 변경안 전체를 검토한 뒤 GitHub
        Draft PR로 제출할 수 있습니다.
      </p>
      <div className="analysis-controls">
        <label>
          분석 대상 환경
          <select
            value={target}
            disabled={waiting || !!pending.current}
            onChange={(e) => setTarget(e.target.value)}
          >
            <option value="aws">AWS</option>
            <option value="onprem">온프레미스</option>
          </select>
        </label>
        <button className="primary" disabled={waiting || activeAnalysis} onClick={start}>
          <Sparkles size={16} />
          {pending.current ? '분석 요청 재시도' : run ? '최신 코드로 새 분석' : '분석 시작'}
        </button>
      </div>
      {history.length > 0 && (
        <label>
          최근 분석 이력
          <select
            aria-label="분석 이력"
            disabled={busy || loading || Boolean(run?.busy)}
            value={run?.id ?? ''}
            onChange={(e) => select(e.target.value)}
          >
            {history.map((row) => (
              <option key={row.id} value={row.id}>
                {new Date(row.created_at * 1000).toLocaleString('ko-KR')} ·{' '}
                {row.base_sha.slice(0, 8)} · {label(row.status)}
              </option>
            ))}
          </select>
        </label>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {run?.error && run.error !== error && (
        <p className="error" role="alert">
          {run.error}
        </p>
      )}
      <div aria-live="polite">
        {waiting && (
          <p className="workflow-busy">
            <LoaderCircle size={16} className="spin" />{' '}
            {loading
              ? '분석 이력을 불러오는 중입니다.'
              : '작업을 처리 중입니다. 새로고침해도 이력은 유지됩니다.'}
          </p>
        )}
      </div>
      {run && (
        <>
          <p className="small quiet">
            작업: {label(run.status)} · {providers[run.provider]} · {run.target_env} · 기준{' '}
            {run.base_branch} <code>{run.base_sha.slice(0, 12)}</code>
          </p>
          {run.logs.length > 0 && (
            <details>
              <summary>진행 기록</summary>
              <ol>
                {run.logs.map((event, i) => (
                  <li key={i}>
                    {new Date(event.at * 1000).toLocaleTimeString('ko-KR')} · {event.stage}
                  </li>
                ))}
              </ol>
            </details>
          )}
          {diagnosis && (
            <>
              <div className="analysis-summary">
                <div>
                  <small>분석 결과</small>
                  <strong>{label(report?.status)}</strong>
                </div>
                <div>
                  <small>지원 범위</small>
                  <strong>{label(diagnosis.support_grade)}</strong>
                </div>
                <div>
                  <small>규칙 진단</small>
                  <strong>{diagnosis.violations.length}개</strong>
                </div>
                <div>
                  <small>반영 / 보류</small>
                  <strong>
                    {transform?.addressed_ids.length ?? 0} / {transform?.deferred_ids.length ?? 0}
                  </strong>
                </div>
              </div>
              <p>{diagnosis.framework?.reason}</p>
              {diagnosis.enrichment_status === 'failed' && (
                <p className="error">
                  모델 보강에 실패했습니다. 아래 결과의 지원 범위와 경고를 확인해 주세요.
                </p>
              )}
              <h3>규칙 기반 진단</h3>
              {diagnosis.violations.length === 0 && (
                <p>규칙에서 발견한 항목이 없습니다. 전체 기능의 정상 동작을 보장하지는 않습니다.</p>
              )}
              {diagnosis.violations.map((item) => (
                <details key={item.id}>
                  <summary>
                    Factor {item.factor} · {item.rule} ·{' '}
                    {transform?.addressed_ids.includes(item.id) ? '변경안 반영' : '미반영'}
                    {item.change_class === 'risky' ? ' · 위험 변경' : ''}
                  </summary>
                  <p>
                    {item.file}:{item.line} · {item.description}
                  </p>
                  <pre>{item.evidence}</pre>
                </details>
              ))}
              {diagnosis.review_candidates.length > 0 && (
                <>
                  <h3>AI 추가 검토 후보</h3>
                  <p className="small">확정된 위반이나 자동 수정 항목이 아닙니다.</p>
                  {diagnosis.review_candidates.map((item) => (
                    <details key={item.id}>
                      <summary>{item.description}</summary>
                      <p>
                        Factor {item.factor} · {item.file}:{item.line}
                      </p>
                      <pre>{item.evidence}</pre>
                    </details>
                  ))}
                </>
              )}
            </>
          )}
          {report?.recommendation && (
            <div className="analysis-note">
              <h3>배포 방식 제안</h3>
              <p>
                {report.recommendation.set ?? '추천 없음'} · {report.recommendation.rationale}
              </p>
              <p className="small">
                예상 월 비용:{' '}
                {report.recommendation.estimated_monthly_cost == null
                  ? '산정되지 않음'
                  : `$${report.recommendation.estimated_monthly_cost}`}{' '}
                · 배포 명세 호환: {report.adapter_compatible ? '확인' : '미확인'}
              </p>
              <p className="small">
                PR 병합 후 배포 화면에서 별도로 진행합니다. 현재 웹 배포는 AWS 상시 실행 방식만
                지원합니다.
              </p>
            </div>
          )}
          {gate && (
            <div className="analysis-note">
              <h3>검증 범위</h3>
              <p>
                패치: {transform?.patch_valid ? '확인' : '미확인'} · Python 문법:{' '}
                {transform?.compile_passed ? '확인' : '미확인'} · 실행 검증: {label(gate.status)}
              </p>
              <p className="small">
                {gate.reason} · 자동 PR 적격: {gate.pr_eligible ? '예' : '아니요'}
                {gate.historical_status ? ` · 과거 결과: ${gate.historical_status}` : ''}
              </p>
              <p className="small">
                이 화면에서는 앱 빌드·기동을 실행하지 않습니다. Draft PR은 사용자 검토를 위한
                제안입니다.
              </p>
            </div>
          )}
          {cost && (
            <p className="small quiet">
              모델 비용:{' '}
              {cost.total.cost_usd == null
                ? '일부 비용 미확인'
                : `$${cost.total.cost_usd.toFixed(6)}`}{' '}
              · 외부 호출: {cost.external_calls ?? '미집계'}
              {!cost.total.usage_complete || !cost.total.pricing_complete ? ' · 집계 불완전' : ''}
              {cost.historical ? ' · 과거 실행 비용' : ''}
            </p>
          )}
          {!!report?.source_skipped_files && (
            <p className="small quiet">분석에서 제외된 파일: {report.source_skipped_files}개</p>
          )}
          {warnings.length > 0 && (
            <details>
              <summary>분석 경고 {warnings.length}개</summary>
              <ul>
                {warnings.map((w, i) => (
                  <li key={i}>
                    {w.code}: {w.message}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {run.files.length > 0 && (
            <>
              <h3>변경안 전체 · {run.files.length}개 파일</h3>
              <FileDiffViewer key={`${run.id}:${run.review_hash}`} diff={run.diff} />
            </>
          )}
          {run.status === 'completed' && run.files.length === 0 && (
            <p>게시할 변경안이 없습니다. 진단 결과와 경고를 확인해 주세요.</p>
          )}
          {publishable && run.publish_status !== 'pr_created' && (
            <>
              <label className="review-check">
                <input
                  type="checkbox"
                  checked={reviewed}
                  disabled={waiting}
                  onChange={(e) => setReviewed(e.target.checked)}
                />
                <span>
                  전체 변경안을 검토했습니다. 연결된 GitHub 저장소에 브랜치·커밋과 Draft PR을
                  생성합니다.
                </span>
              </label>
              {transform?.needs_approval && (
                <label className="review-check">
                  <input
                    type="checkbox"
                    checked={risky}
                    disabled={waiting}
                    onChange={(e) => setRisky(e.target.checked)}
                  />
                  <span>
                    DB·저장 방식 변경을 승인합니다. 기존 데이터의 자동 이전은 지원하지 않으며 별도
                    이전 계획이 필요합니다.
                  </span>
                </label>
              )}
              <button
                className="primary"
                disabled={waiting || !reviewed || (!!transform?.needs_approval && !risky)}
                onClick={publish}
              >
                <GitPullRequest size={16} />
                GitHub Draft PR 생성
              </button>
            </>
          )}
          {run.publish_status === 'pr_created' && (
            <div className="published-pr">
              <h3>Draft PR #{run.pr_number} 생성 완료</h3>
              <a className="primary" href={run.pr_url} target="_blank" rel="noreferrer">
                GitHub에서 PR 검토 <ExternalLink size={16} />
              </a>
            </div>
          )}
        </>
      )}
    </section>
  );
}
