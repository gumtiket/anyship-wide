import { useEffect, useRef, useState } from 'react';
import { Check, FlaskConical, LoaderCircle, Play, RefreshCw, RotateCcw } from 'lucide-react';
import { api, mutation } from './api';
import { DeleteRegistration } from './DeleteRegistration';
import { DeploymentLogs } from './DeploymentLogs';
import './mock-deployment.css';

type Target = {
  source: string;
  label: string;
  kind: string;
  set_name: string;
  checked: boolean;
  active_job_id: string | null;
  state: string;
  image_tag: string | null;
  example_url: string | null;
  versions: string[];
};
type Environment = {
  source: string;
  label: string;
  kind: string;
  sets: string[];
  available: boolean;
};
type Configuration = { mock: boolean; target: Target | null; environments: Environment[] };
type Action = 'check' | 'deploy' | 'rollback' | 'destroy';
type Job = {
  id: string;
  request_id: string;
  action: Action;
  scenario: string;
  status: string;
  image_tag: string;
  target_label: string;
  set_name: string;
  created_at: number;
  logs: { ts: string; step: number; total: number; name: string; message: string; level: string }[];
  result: {
    ok?: boolean;
    error?: { message: string; hint?: string; retryable?: boolean };
    url?: string;
  };
};
const sets: Record<string, string> = {
  'aws-serverless': 'AWS 서버리스',
  'aws-always-on': 'AWS 상시 실행',
  onprem: '온프레미스',
};
const actions: Record<Action, string> = {
  check: '연결 확인',
  deploy: '배포',
  rollback: '롤백',
  destroy: '배포 제거',
};
const states: Record<string, string> = {
  queued: '대기',
  running: '진행 중',
  succeeded: '성공',
  failed: '실패',
  interrupted: '중단',
};

export function MockDeployment({
  projectId,
  csrf,
  mode,
  onError,
}: {
  projectId: string;
  csrf: string;
  mode: string;
  onError: (error: unknown) => void;
}) {
  const endpoint = `/projects/${projectId}/mock-deployment`;
  const [config, setConfig] = useState<Configuration | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [source, setSource] = useState('');
  const [setName, setSetName] = useState('');
  const [scenario, setScenario] = useState('success');
  const [version, setVersion] = useState('aaaaaaa');
  const [rollback, setRollback] = useState('');
  const [selectedJob, setSelectedJob] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const mounted = useRef(false);
  const inFlight = useRef(false);
  const pending = useRef<{ key: string; requestId: string } | null>(null);
  const target = config?.target;
  const active = Boolean(
    target?.active_job_id || jobs.some((job) => ['queued', 'running'].includes(job.status)),
  );
  const waiting = busy || active;
  const dirty = !target || target.source !== source || target.set_name !== setName;
  const job = jobs.find((item) => item.id === selectedJob) ?? jobs[0];
  const environment = config?.environments.find((item) => item.source === source);
  const previousVersions = target?.versions.filter((tag) => tag !== target.image_tag) ?? [];
  const rollbackVersion = previousVersions.includes(rollback)
    ? rollback
    : (previousVersions[0] ?? '');

  async function refresh() {
    const [configuration, history] = await Promise.all([
      api<Configuration>(endpoint),
      api<Job[]>(endpoint + '/jobs'),
    ]);
    if (!mounted.current) return;
    setConfig(configuration);
    setJobs(history);
    setSource((value) => value || configuration.target?.source || 'sample-aws');
    setSetName((value) => value || configuration.target?.set_name || 'aws-always-on');
    if (history.some((item) => item.request_id === pending.current?.requestId))
      pending.current = null;
  }
  function report(e: unknown) {
    if (!mounted.current) return;
    setError(e instanceof Error ? e.message : '모의 작업을 불러오지 못했습니다.');
    onError(e);
  }
  useEffect(() => {
    mounted.current = true;
    if (mode === 'mock') void refresh().catch(report);
    return () => {
      mounted.current = false;
    };
  }, [endpoint, mode]);
  useEffect(() => {
    if (!active) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        await refresh();
      } catch (e) {
        if (!stopped) report(e);
      }
      if (!stopped) timer = setTimeout(poll, 1000);
    }
    timer = setTimeout(poll, 600);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [active, endpoint]);

  async function save() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await api(endpoint, mutation(csrf, { source, set_name: setName }, 'PUT'));
      await refresh();
      setNotice('테스트 환경을 저장했습니다. 모의 연결 확인부터 시작하세요.');
    } catch (e) {
      report(e);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  async function run(action: Action) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError('');
    setNotice('');
    const imageTag = action === 'deploy' ? version : action === 'rollback' ? rollbackVersion : '';
    const key = JSON.stringify([action, scenario, imageTag, target?.source, target?.set_name]);
    if (pending.current?.key !== key) pending.current = { key, requestId: crypto.randomUUID() };
    try {
      const accepted = await api<Job>(
        endpoint + '/jobs',
        mutation(csrf, {
          request_id: pending.current.requestId,
          action,
          scenario,
          image_tag: imageTag,
        }),
      );
      pending.current = null;
      setSelectedJob(accepted.id);
      setJobs((items) => [accepted, ...items.filter((item) => item.id !== accepted.id)]);
      await refresh();
    } catch (e) {
      report(e);
      try {
        await refresh();
      } catch {
        /* 재시도를 위해 처음 오류와 요청 ID를 유지한다. */
      }
      throw e;
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  if (mode !== 'mock') return null;
  return (
    <section className="panel mock-deployment" aria-label="모의 배포">
      <div className="panel-heading">
        <h2>
          <FlaskConical size={19} /> 모의 배포
        </h2>
        <span className="mock-badge">개발용 테스트</span>
      </div>
      <p className="mock-description">
        실제 클라우드 작업 없이 연결 확인부터 배포·롤백까지 시험합니다. AWS 연결 완료 상태는 바뀌지
        않습니다.
      </p>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <p className="mock-notice" role="status">
          <Check size={15} />
          {notice}
        </p>
      )}
      {!config ? (
        <p role="status">
          {error ? '잠시 후 다시 불러와 주세요.' : '테스트 환경을 불러오고 있습니다.'}
        </p>
      ) : (
        <>
          <div className="mock-fields">
            <label>
              테스트 환경
              <select
                aria-label="테스트 환경"
                value={source}
                disabled={waiting}
                onChange={(event) => {
                  setSource(event.target.value);
                  setSetName(
                    config.environments.find((item) => item.source === event.target.value)
                      ?.sets[0] ?? '',
                  );
                }}
              >
                {config.environments.map((item) => (
                  <option key={item.source} value={item.source} disabled={!item.available}>
                    {item.label}
                    {item.available ? '' : ' · ARN 저장 필요'}
                  </option>
                ))}
              </select>
            </label>
            <label>
              배포 방식
              <select
                aria-label="배포 방식"
                value={setName}
                disabled={waiting}
                onChange={(event) => setSetName(event.target.value)}
              >
                {environment?.sets.map((name) => (
                  <option key={name} value={name}>
                    {sets[name]}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="mock-toolbar">
            <button
              className="secondary"
              disabled={waiting || !environment?.available || !dirty}
              onClick={() => void save()}
            >
              테스트 환경 저장
            </button>
            {dirty && <span className="quiet">선택한 환경을 저장해 주세요.</span>}
          </div>
          {target && (
            <>
              <div className="mock-current">
                <strong>{target.label}</strong>
                <span>{sets[target.set_name]}</span>
                <span className="status">
                  {target.state === 'running' ? '모의 실행 중' : '모의 배포 없음'}
                </span>
                <small>
                  {target.checked ? '모의 연결 확인 완료' : '모의 연결 확인 필요'}
                  {target.image_tag ? ` · 현재 버전 ${target.image_tag}` : ''}
                </small>
                {target.example_url && (
                  <p>
                    예시 주소 <code>{target.example_url}</code>
                    <small>주소 형식만 보여 줍니다. 실제 접속 주소가 아닙니다.</small>
                  </p>
                )}
              </div>
              <div className="mock-fields">
                <label>
                  테스트 상황
                  <select
                    aria-label="테스트 상황"
                    value={scenario}
                    disabled={waiting}
                    onChange={(event) => setScenario(event.target.value)}
                  >
                    <option value="success">정상 성공</option>
                    <option value="check_fails">환경 연결 실패</option>
                    <option value="deploy_fails">앱 시작 실패</option>
                    <option value="unhealthy">헬스체크 실패</option>
                  </select>
                </label>
                <label>
                  배포할 테스트 버전
                  <input
                    aria-label="배포할 테스트 버전"
                    value={version}
                    disabled={waiting}
                    onChange={(event) => setVersion(event.target.value)}
                    maxLength={40}
                    placeholder="예: aaaaaaa, bbbbbbb"
                  />
                  <small>7~40자리 16진수. 테스트용이며 이미지를 빌드하지 않습니다.</small>
                </label>
              </div>
              <div className="mock-toolbar">
                <button
                  className="secondary"
                  disabled={waiting || dirty}
                  onClick={() => void run('check').catch(() => {})}
                >
                  <Check size={15} /> 모의 연결 확인
                </button>
                <button
                  className="primary"
                  disabled={
                    waiting || dirty || !target.checked || !/^[0-9a-f]{7,40}$/.test(version)
                  }
                  onClick={() => void run('deploy').catch(() => {})}
                >
                  <Play size={15} /> 모의 배포
                </button>
                {waiting && (
                  <span role="status">
                    <LoaderCircle size={15} className="spin" /> 작업을 처리하고 있습니다.
                  </span>
                )}
              </div>
              {target.state === 'running' && (
                <div className="mock-lifecycle">
                  <label>
                    되돌릴 버전
                    <select
                      aria-label="되돌릴 버전"
                      value={rollbackVersion}
                      disabled={waiting || !previousVersions.length}
                      onChange={(event) => setRollback(event.target.value)}
                    >
                      {!previousVersions.length && <option value="">이전 배포 버전 없음</option>}
                      {previousVersions.map((tag) => (
                        <option key={tag} value={tag}>
                          {tag}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    className="secondary"
                    disabled={waiting || dirty || !rollbackVersion}
                    onClick={() => void run('rollback').catch(() => {})}
                  >
                    <RotateCcw size={15} /> 모의 롤백
                  </button>
                  <DeleteRegistration
                    label="모의 배포 제거"
                    name={target.label}
                    disabled={waiting || dirty}
                    description="테스트용 실행 상태를 제거합니다. 등록한 환경과 작업 기록은 유지됩니다. 실제 AWS·서버 리소스에는 영향이 없습니다."
                    onDelete={() => run('destroy')}
                  />
                </div>
              )}
            </>
          )}
          <div className="mock-history">
            <h3>
              작업 기록 <span>최근 50개</span>
            </h3>
            <p className="small quiet">
              기록은 유지되지만, 서버를 재시작하면 모의 실행 상태와 연결 확인은 초기화됩니다.
            </p>
            {!jobs.length ? (
              <p className="quiet">아직 모의 작업이 없습니다.</p>
            ) : (
              <>
                <div className="mock-job-list" aria-label="모의 작업 기록">
                  {jobs.map((item) => (
                    <button
                      key={item.id}
                      className={job?.id === item.id ? 'selected' : ''}
                      onClick={() => setSelectedJob(item.id)}
                    >
                      <span>
                        {actions[item.action]}
                        {item.image_tag ? ` · ${item.image_tag}` : ''}
                      </span>
                      <small>
                        {states[item.status]} ·{' '}
                        {new Date(item.created_at).toLocaleTimeString('ko-KR')}
                      </small>
                    </button>
                  ))}
                </div>
                {job && (
                  <div className="mock-job-detail" aria-live="polite">
                    <strong>
                      {actions[job.action]} · {states[job.status]}
                    </strong>
                    <p>
                      {job.target_label} · {sets[job.set_name]}
                    </p>
                    {job.result.error && (
                      <div className="error" role="alert">
                        <div>
                          {job.result.error.message}
                          {job.result.error.hint && <small>{job.result.error.hint}</small>}
                        </div>
                      </div>
                    )}
                    <DeploymentLogs key={job.id} logs={job.logs} />
                    {job.status === 'queued' && <p>작업 순서를 기다리고 있습니다.</p>}
                  </div>
                )}
              </>
            )}
          </div>
        </>
      )}
      <button
        className="text-link mock-refresh"
        disabled={busy}
        onClick={() => {
          setError('');
          void refresh().catch(report);
        }}
      >
        <RefreshCw size={14} /> 상태 새로고침
      </button>
    </section>
  );
}
