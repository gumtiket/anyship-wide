import { useEffect, useRef, useState } from 'react';
import { Check, ExternalLink, LoaderCircle, Plus, RefreshCw, Rocket, X } from 'lucide-react';
import { api, mutation } from './api';
import { DeleteRegistration } from './DeleteRegistration';
import { DeploymentLogs } from './DeploymentLogs';
import { MigrateEnvironment, type MigrationTarget } from './MigrateEnvironment';
import './mock-deployment.css';
import './deployment.css';

type Previous = {
  kind: string;
  environment_name: string;
  set_name: string;
  app_name: string;
  url: string;
};
type Target = {
  environment_id: string;
  environment_name: string;
  set_name: string;
  app_name: string;
  image_tag: string;
  url: string;
  deployed: boolean;
  active_job_id: string | null;
  busy: boolean;
  previous: Previous | null;
};
type Environment = {
  id: string;
  name: string;
  region: string;
  available: boolean;
  set_name: string;
};
type Overview = { target: Target | null; sets: string[]; environments: Environment[] };
type Job = {
  id: string;
  request_id: string;
  action: 'deploy' | 'destroy' | 'status' | 'rollback' | 'migrate' | 'destroy_previous';
  set_name: string;
  image_tag: string;
  status: string;
  stage: string;
  created_at: number;
  logs: { step: number; total: number; name: string; message: string; level: string }[];
  result: {
    ok?: boolean;
    state?: string;
    url?: string;
    error?: { message: string; hint?: string | null; retryable?: boolean };
    details?: { target_left?: boolean; tables?: number; rows?: number; bytes?: number };
  };
};
type Secret = { key: number; name: string; value: string };

const sets: Record<string, string> = { 'aws-always-on': 'AWS 상시 실행', onprem: '온프레미스' };
const actions: Record<string, string> = {
  deploy: '배포',
  destroy: '배포 제거',
  status: '상태 확인',
  rollback: '롤백',
  migrate: '환경 이전',
  destroy_previous: '이전 앱 삭제',
};
const states: Record<string, string> = {
  queued: '대기',
  running: '진행 중',
  succeeded: '성공',
  failed: '실패',
  interrupted: '중단',
};
const stages: Record<string, string> = {
  source: '소스 확인',
  spec: '배포 명세 검사',
  build: '이미지 빌드',
  foundation: '공용 기반 확인',
  check: '연결 확인',
  deploy: '배포',
  destroy: '앱 삭제',
  environment: '환경 준비',
  runner: '실행기',
  preflight: '원본 확인',
  transfer: '데이터 이전',
};
const SECRET_NAME = /^[A-Z_][A-Z0-9_]{0,63}$/;

export function Deployment({
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
  const endpoint = `/projects/${projectId}/deployment`;
  const [overview, setOverview] = useState<Overview | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [environmentId, setEnvironmentId] = useState('');
  const [secrets, setSecrets] = useState<Secret[]>([]);
  const [selectedJob, setSelectedJob] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [rollbackTag, setRollbackTag] = useState('');
  const mounted = useRef(false);
  const inFlight = useRef(false);
  const nextKey = useRef(0);
  const pending = useRef<{ key: string; requestId: string } | null>(null);
  const target = overview?.target;
  const selectedSet =
    overview?.environments.find((item) => item.id === environmentId)?.set_name ??
    target?.set_name ??
    'aws-always-on';
  const active = Boolean(
    target?.active_job_id || jobs.some((job) => ['queued', 'running'].includes(job.status)),
  );
  const waiting = busy || active;
  const dirty = !target || target.environment_id !== environmentId;
  const job = jobs.find((item) => item.id === selectedJob) ?? jobs[0];
  const names = secrets.map((item) => item.name.trim()).filter(Boolean);
  const secretsValid =
    secrets.every(
      (item) =>
        (!item.name.trim() && !item.value) ||
        (SECRET_NAME.test(item.name.trim()) && item.value.length <= 4096),
    ) && new Set(names).size === names.length;

  async function refresh() {
    const [current, history] = await Promise.all([
      api<Overview>(endpoint),
      api<Job[]>(endpoint + '/jobs'),
    ]);
    if (!mounted.current) return;
    setOverview(current);
    setJobs(history);
    setEnvironmentId((value) =>
      current.target?.deployed
        ? current.target.environment_id
        : value ||
          current.target?.environment_id ||
          current.environments.find((item) => item.available)?.id ||
          '',
    );
    if (history.some((item) => item.request_id === pending.current?.requestId))
      pending.current = null;
  }
  function report(e: unknown) {
    if (!mounted.current) return;
    setError(e instanceof Error ? e.message : '배포 정보를 불러오지 못했습니다.');
    onError(e);
  }
  useEffect(() => {
    mounted.current = true;
    if (mode === 'real') void refresh().catch(report);
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
      if (!stopped) timer = setTimeout(poll, 2000);
    }
    timer = setTimeout(poll, 1000);
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
      await api(
        endpoint,
        mutation(csrf, { environment_id: environmentId, set_name: selectedSet }, 'PUT'),
      );
      await refresh();
      setNotice('배포할 환경을 저장했습니다.');
    } catch (e) {
      report(e);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  async function deploy() {
    if (inFlight.current || !target) return;
    inFlight.current = true;
    setBusy(true);
    setError('');
    setNotice('');
    const body: Record<string, string> = {};
    for (const item of secrets) if (item.name.trim()) body[item.name.trim()] = item.value;
    // 응답이 유실되면 같은 내용으로 다시 보내도 같은 작업이 된다. 비밀이 든 키는 메모리에만 둔다.
    const key = JSON.stringify([target.environment_id, body]);
    if (pending.current?.key !== key) pending.current = { key, requestId: crypto.randomUUID() };
    try {
      const accepted = await api<Job>(
        endpoint + '/jobs',
        mutation(csrf, { request_id: pending.current.requestId, secrets: body }),
      );
      pending.current = null;
      setSecrets([]); // 비밀 값은 보낸 즉시 화면에서도 지운다
      setSelectedJob(accepted.id);
      setJobs((items) => [accepted, ...items.filter((item) => item.id !== accepted.id)]);
      await refresh();
    } catch (e) {
      report(e);
      try {
        await refresh();
      } catch {
        /* 처음 오류와 요청 ID를 유지한다. */
      }
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  // 배포한 앱을 지운다(앱 컨테이너와 서버의 앱 폴더만). 확인 대화상자가 오류를 보여 주도록 실패는 다시 던진다.
  async function removeApp() {
    if (inFlight.current || !target) return;
    inFlight.current = true;
    setBusy(true);
    setError('');
    setNotice('');
    const key = JSON.stringify(['destroy', target.environment_id, target.image_tag]);
    if (pending.current?.key !== key) pending.current = { key, requestId: crypto.randomUUID() };
    try {
      const accepted = await api<Job>(
        endpoint + '/jobs',
        mutation(csrf, { request_id: pending.current.requestId, action: 'destroy', secrets: {} }),
      );
      pending.current = null;
      setSelectedJob(accepted.id);
      setJobs((items) => [accepted, ...items.filter((item) => item.id !== accepted.id)]);
      await refresh();
      setNotice('배포 제거를 시작했습니다. 진행 상황은 아래 기록에서 확인하세요.');
    } catch (e) {
      report(e);
      try {
        await refresh();
      } catch {
        /* 처음 오류와 요청 ID를 유지한다. */
      }
      throw e;
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  // 다른 환경으로 옮긴다. 확인 대화상자가 오류를 보여 주도록 실패는 다시 던진다. 비밀 값은 보낸 즉시 화면에서 지운다.
  async function migrateTo(destination: MigrationTarget) {
    if (inFlight.current || !target) return;
    inFlight.current = true;
    setBusy(true);
    setError('');
    setNotice('');
    const body: Record<string, string> = {};
    for (const item of secrets) if (item.name.trim()) body[item.name.trim()] = item.value;
    const key = JSON.stringify(['migrate', target.environment_id, destination.id, body]);
    if (pending.current?.key !== key) pending.current = { key, requestId: crypto.randomUUID() };
    try {
      const accepted = await api<Job>(
        endpoint + '/jobs',
        mutation(csrf, {
          request_id: pending.current.requestId,
          action: 'migrate',
          target_environment_id: destination.id,
          target_set_name: destination.set_name,
          secrets: body,
        }),
      );
      pending.current = null;
      setSecrets([]);
      setSelectedJob(accepted.id);
      setJobs((items) => [accepted, ...items.filter((item) => item.id !== accepted.id)]);
      await refresh();
      setNotice('환경 이전을 시작했습니다. 진행 상황은 아래 기록에서 확인하세요.');
    } catch (e) {
      report(e);
      try {
        await refresh();
      } catch {
        /* 처음 오류와 요청 ID를 유지한다. */
      }
      throw e;
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  // 환경 이전 뒤 원래 환경에 멈춘 채 남은 앱을 지운다.
  async function removePrevious() {
    if (inFlight.current || !target) return;
    inFlight.current = true;
    setBusy(true);
    setError('');
    setNotice('');
    const key = JSON.stringify([
      'destroy_previous',
      target.environment_id,
      target.previous?.app_name,
    ]);
    if (pending.current?.key !== key) pending.current = { key, requestId: crypto.randomUUID() };
    try {
      const accepted = await api<Job>(
        endpoint + '/jobs',
        mutation(csrf, {
          request_id: pending.current.requestId,
          action: 'destroy_previous',
          secrets: {},
        }),
      );
      pending.current = null;
      setSelectedJob(accepted.id);
      setJobs((items) => [accepted, ...items.filter((item) => item.id !== accepted.id)]);
      await refresh();
      setNotice('이전 앱 삭제를 시작했습니다. 진행 상황은 아래 기록에서 확인하세요.');
    } catch (e) {
      report(e);
      try {
        await refresh();
      } catch {
        /* 처음 오류와 요청 ID를 유지한다. */
      }
      throw e;
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  function edit(key: number, change: Partial<Secret>) {
    setSecrets((items) => items.map((item) => (item.key === key ? { ...item, ...change } : item)));
  }
  async function lifecycle(action: 'status' | 'rollback') {
    if (inFlight.current || !target) return;
    inFlight.current = true;
    setBusy(true);
    setError('');
    const imageTag = action === 'rollback' ? rollbackTag.trim() : '';
    const key = JSON.stringify([action, target.environment_id, imageTag]);
    if (pending.current?.key !== key) pending.current = { key, requestId: crypto.randomUUID() };
    try {
      const accepted = await api<Job>(
        endpoint + '/jobs',
        mutation(csrf, {
          request_id: pending.current.requestId,
          action,
          image_tag: imageTag,
        }),
      );
      pending.current = null;
      setSelectedJob(accepted.id);
      await refresh();
    } catch (e) {
      report(e);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  if (mode !== 'real') return null;
  const connectable = overview?.environments.filter((item) => item.available) ?? [];
  const candidates: MigrationTarget[] = connectable.filter(
    (item) => item.id !== target?.environment_id,
  );
  return (
    <section className="panel mock-deployment" aria-label="배포">
      <div className="panel-heading">
        <h2>
          <Rocket size={19} /> 배포
        </h2>
        <span className="mock-badge">{sets[selectedSet]}</span>
      </div>
      <p className="mock-description">
        {selectedSet === 'onprem'
          ? '연결이 확인된 내 서버에 앱을 배포하고 공개 주소를 받습니다.'
          : '연결한 AWS 계정에 이미지를 만들어 올리고 공개 주소를 받습니다. 첫 배포는 서버와 DB를 새로 만드느라 약 20분이 걸릴 수 있습니다.'}
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
      {!overview ? (
        <p role="status">
          {error ? '잠시 후 다시 불러와 주세요.' : '배포 정보를 불러오고 있습니다.'}
        </p>
      ) : (
        <>
          {connectable.length === 0 ? (
            <p className="quiet">
              연결이 확인된 환경이 없습니다.{' '}
              <a className="text-link" href="#aws">
                AWS 환경
              </a>{' '}
              또는{' '}
              <a className="text-link" href="#onprem">
                온프레미스 환경
              </a>
              에서 연결을 먼저 완료하세요.
            </p>
          ) : (
            <>
              <div className="mock-fields">
                <label>
                  배포할 환경
                  <select
                    aria-label="배포할 환경"
                    value={environmentId}
                    disabled={waiting || Boolean(target?.deployed)}
                    onChange={(event) => setEnvironmentId(event.target.value)}
                  >
                    {overview.environments.map((item) => (
                      <option key={item.id} value={item.id} disabled={!item.available}>
                        {item.name} · {item.region}
                        {item.available ? '' : ' · 연결 확인 필요'}
                      </option>
                    ))}
                  </select>
                  {target?.deployed && <small>한 번 배포한 뒤에는 환경을 바꿀 수 없습니다.</small>}
                </label>
                <label>
                  배포 방식
                  <select aria-label="배포 방식" value={selectedSet} disabled>
                    {overview.sets.map((name) => (
                      <option key={name} value={name}>
                        {sets[name] ?? name}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="mock-toolbar">
                <button
                  className="secondary"
                  disabled={waiting || !dirty || !environmentId}
                  onClick={() => void save()}
                >
                  배포 환경 저장
                </button>
                {dirty && <span className="quiet">선택한 환경을 저장해 주세요.</span>}
              </div>
            </>
          )}
          {target && (
            <>
              <div className="mock-current">
                <strong>{target.environment_name}</strong>
                <span>{sets[target.set_name] ?? target.set_name}</span>
                <span className="status">{target.deployed ? '배포됨' : '아직 배포하지 않음'}</span>
                {target.image_tag && (
                  <small>
                    현재 버전 <code>{target.image_tag}</code>
                    {target.app_name ? ` · 앱 ${target.app_name}` : ''}
                  </small>
                )}
                {target.url && (
                  <p>
                    공개 주소{' '}
                    <a
                      className="deploy-url"
                      href={target.url}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {target.url} <ExternalLink size={13} />
                    </a>
                  </p>
                )}
              </div>
              <details className="deploy-secrets">
                <summary>앱에 넣을 비밀 값 {names.length > 0 && `(${names.length}개)`}</summary>
                <p className="small quiet">
                  배포 명세가 요구하는 비밀(예: API 키)만 입력하세요. 값은 이 요청에만 쓰이고
                  저장되지 않으며, 보낸 뒤 화면에서도 지워집니다. 다음 배포 때 다시 입력해야 합니다.
                </p>
                {secrets.map((item) => (
                  <div className="secret-row" key={item.key}>
                    <input
                      aria-label="비밀 이름"
                      placeholder="이름 (예: API_KEY)"
                      value={item.name}
                      disabled={waiting}
                      autoComplete="off"
                      onChange={(event) =>
                        edit(item.key, { name: event.target.value.toUpperCase() })
                      }
                    />
                    <input
                      aria-label="비밀 값"
                      type="password"
                      placeholder="값"
                      value={item.value}
                      disabled={waiting}
                      autoComplete="new-password"
                      onChange={(event) => edit(item.key, { value: event.target.value })}
                    />
                    <button
                      className="text-link"
                      aria-label="비밀 항목 삭제"
                      disabled={waiting}
                      onClick={() =>
                        setSecrets((items) => items.filter((other) => other.key !== item.key))
                      }
                    >
                      <X size={15} />
                    </button>
                  </div>
                ))}
                <button
                  className="secondary"
                  disabled={waiting || secrets.length >= 50}
                  onClick={() =>
                    setSecrets((items) => [
                      ...items,
                      { key: nextKey.current++, name: '', value: '' },
                    ])
                  }
                >
                  <Plus size={14} /> 비밀 추가
                </button>
                {!secretsValid && (
                  <p className="small error-text" role="alert">
                    이름은 대문자·숫자·밑줄만 쓰고(숫자로 시작 불가) 중복 없이 입력하세요. 값은
                    4096자 이하여야 합니다.
                  </p>
                )}
              </details>
              <div className="mock-toolbar">
                <button
                  className="primary"
                  disabled={waiting || dirty || !secretsValid}
                  onClick={() => void deploy()}
                >
                  <Rocket size={15} /> {target.deployed ? '다시 배포' : '배포하기'}
                </button>
                {waiting && (
                  <span role="status">
                    <LoaderCircle size={15} className="spin" /> 작업을 진행하고 있습니다. 창을
                    닫아도 계속 진행됩니다.
                  </span>
                )}
              </div>
              {target.deployed && (
                <div className="mock-lifecycle">
                  {target.set_name === 'onprem' && (
                    <div className="mock-toolbar">
                      <button
                        className="secondary"
                        disabled={waiting || dirty}
                        onClick={() => void lifecycle('status')}
                      >
                        앱 상태 확인
                      </button>
                      <label>
                        롤백할 커밋 SHA
                        <input
                          aria-label="롤백할 커밋 SHA"
                          value={rollbackTag}
                          disabled={waiting}
                          onChange={(event) => setRollbackTag(event.target.value)}
                          maxLength={40}
                        />
                      </label>
                      <button
                        className="secondary"
                        disabled={waiting || dirty || !/^[0-9a-f]{7,40}$/.test(rollbackTag.trim())}
                        onClick={() => void lifecycle('rollback')}
                      >
                        이 버전으로 롤백
                      </button>
                      <p className="quiet">
                        서버에 남은 이미지로 되돌립니다. DB 스키마는 되돌리지 않습니다.
                      </p>
                    </div>
                  )}
                  <DeleteRegistration
                    label="배포 제거"
                    name={target.url || target.app_name}
                    disabled={waiting || dirty}
                    description={
                      target.set_name === 'onprem'
                        ? '이 앱의 컨테이너·볼륨·파일을 삭제합니다. 앱의 데이터도 삭제되므로 필요한 백업을 먼저 확보하세요. 서버와 환경 DNS는 유지됩니다.'
                        : '이 앱의 컨테이너와 서버의 앱 폴더를 지웁니다. 앱 데이터베이스(DB)와 공용 기반(호스트, RDS)은 AWS 계정에 남아 요금이 계속 나옵니다. AnyShip의 프로젝트 연결은 유지되고 다시 배포할 수 있습니다.'
                    }
                    onDelete={removeApp}
                  />
                </div>
              )}
              {target.deployed && (
                <div className="mock-lifecycle migrate-block" aria-label="환경 이전">
                  {target.previous ? (
                    <div className="migrate-previous" role="status">
                      <strong>원래 환경에 멈춘 앱이 남아 있습니다</strong>
                      {target.previous.environment_name} · 앱 {target.previous.app_name}
                      {target.previous.url && <> · 이전 주소 {target.previous.url}</>}
                      <p className="quiet">
                        새 주소에서 앱과 데이터를 확인한 뒤 지워 주세요. 지우기 전에는 다른 환경으로
                        다시 옮길 수 없습니다.
                      </p>
                      <div className="migrate-previous-actions">
                        <DeleteRegistration
                          label="이전 앱 제거"
                          name={`${target.previous.environment_name} · ${target.previous.app_name}`}
                          disabled={waiting}
                          description="원래 환경에 멈춰 있는 앱의 컨테이너와 데이터를 지웁니다. 되돌릴 수 없으므로 새 주소에서 데이터를 확인한 뒤에만 지우세요. 앱 DB가 AWS의 공용 RDS에 있었다면 DB는 남습니다."
                          onDelete={removePrevious}
                        />
                      </div>
                    </div>
                  ) : (
                    <MigrateEnvironment
                      currentName={target.environment_name}
                      candidates={candidates}
                      secretCount={names.length}
                      disabled={waiting || dirty || !secretsValid}
                      onMigrate={migrateTo}
                    />
                  )}
                </div>
              )}
            </>
          )}
          <div className="mock-history">
            <h3>
              배포 기록 <span>최근 50개</span>
            </h3>
            {!jobs.length ? (
              <p className="quiet">아직 배포한 기록이 없습니다.</p>
            ) : (
              <>
                <div className="mock-job-list" aria-label="배포 기록">
                  {jobs.map((item) => (
                    <button
                      key={item.id}
                      className={job?.id === item.id ? 'selected' : ''}
                      onClick={() => setSelectedJob(item.id)}
                    >
                      <span>
                        {actions[item.action] ?? '배포'}
                        {item.image_tag ? ` · ${item.image_tag}` : ''}
                      </span>
                      <small>
                        {states[item.status] ?? item.status} ·{' '}
                        {new Date(item.created_at).toLocaleString('ko-KR')}
                      </small>
                    </button>
                  ))}
                </div>
                {job && (
                  <div className="mock-job-detail" aria-live="polite">
                    <strong>
                      {actions[job.action] ?? '배포'} · {states[job.status] ?? job.status}
                    </strong>
                    {job.status === 'failed' && job.stage && (
                      <p>
                        실패한 단계: <strong>{stages[job.stage] ?? job.stage}</strong>
                      </p>
                    )}
                    {job.result.error && (
                      <div className="error" role="alert">
                        <div>
                          {job.result.error.message}
                          {job.result.error.hint && <small>{job.result.error.hint}</small>}
                        </div>
                      </div>
                    )}
                    {job.status === 'succeeded' && job.action === 'destroy' && (
                      <p>
                        {job.set_name === 'onprem'
                          ? '앱과 볼륨을 제거했습니다. 서버와 환경 DNS는 유지됩니다.'
                          : '배포를 제거했습니다. 앱 DB와 공용 기반은 남아 있습니다.'}
                      </p>
                    )}
                    {job.status === 'succeeded' && job.action === 'status' && (
                      <p>앱 상태: {job.result.state}</p>
                    )}
                    {job.status === 'succeeded' && job.action === 'destroy_previous' && (
                      <p>이전 환경의 앱을 지웠습니다.</p>
                    )}
                    {job.status === 'succeeded' && job.action === 'migrate' && (
                      <p>
                        환경 이전을 마쳤습니다
                        {job.result.details?.rows !== undefined &&
                          ` (DB 테이블 ${job.result.details.tables}개 · 행 ${job.result.details.rows}개)`}
                        . 원래 앱은 멈춘 채 남아 있으니 새 주소에서 확인한 뒤 지워 주세요.
                      </p>
                    )}
                    {job.status === 'failed' &&
                      job.action === 'migrate' &&
                      job.result.details?.target_left && (
                        <p>
                          새 환경에는 앱이 남아 있을 수 있습니다. 다시 옮기면 그 위에 새로 배포하고
                          DB를 덮어씁니다. 지금 환경의 앱은 계속 사용할 수 있습니다.
                        </p>
                      )}
                    {job.status === 'succeeded' &&
                      ['deploy', 'rollback', 'migrate'].includes(job.action) &&
                      job.result.url && (
                        <p>
                          {job.action === 'migrate' ? '새 주소' : '배포 완료'}:{' '}
                          <a
                            className="deploy-url"
                            href={job.result.url}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            {job.result.url} <ExternalLink size={13} />
                          </a>
                        </p>
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
