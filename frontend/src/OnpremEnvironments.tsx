import { useEffect, useRef, useState } from 'react';
import { api, mutation } from './api';
import { DeleteRegistration } from './DeleteRegistration';
import { DeploymentLogs } from './DeploymentLogs';
import './aws.css';
import './onprem.css';

type Environment = {
  id: string;
  name: string;
  connection_kind: string;
  status: string;
  public_ip: string | null;
  last_seen_at: number | null;
  active_job_id: string | null;
  error_message: string;
  guidance: { title: string; message: string };
};
type Registration = { command: string; expires_at: number };
type Job = {
  id: string;
  action: string;
  status: string;
  result: { error?: { message: string } };
  logs: { step: number; total: number; name: string; message: string; level: string }[];
};
const states: Record<string, string> = {
  ISSUED: '발급됨 · 서버 준비 대기',
  SIGNALED: '신호 받음 · 연결 확인 필요',
  VERIFIED: '확인됨',
};
const actions: Record<string, string> = { check: '연결 확인', remove_environment: '환경 정리' };

export function OnpremEnvironments({
  csrf,
  available,
  onError,
}: {
  csrf: string;
  available: boolean;
  onError: (error: unknown) => void;
}) {
  const [rows, setRows] = useState<Environment[]>([]);
  const [selected, setSelected] = useState('');
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [registration, setRegistration] = useState<
    (Registration & { environmentId: string }) | null
  >(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [now, setNow] = useState(Date.now());
  const alive = useRef(false),
    locked = useRef(false),
    activeId = useRef(selected);
  const pending = useRef<{ key: string; id: string } | null>(null);
  activeId.current = selected;
  const row = rows.find((item) => item.id === selected);
  const setup = registration?.environmentId === selected ? registration : null;
  const waiting = busy || Boolean(row?.active_job_id);

  function report(e: unknown) {
    if (alive.current) {
      setError(e instanceof Error ? e.message : '환경 정보를 불러오지 못했습니다.');
      onError(e);
    }
  }
  async function refresh() {
    const items = await api<Environment[]>('/onprem/environments');
    if (!alive.current) return;
    setRows(items);
    const id = activeId.current;
    if (id && items.some((item) => item.id === id)) {
      const history = await api<Job[]>(`/onprem/environments/${id}/jobs`);
      if (alive.current && activeId.current === id) setJobs(history);
    } else if (id) {
      setSelected('');
      setJobs([]);
      setRegistration(null);
      setNotice('환경 정리를 완료했습니다. 서버의 공용 프로그램은 유지됩니다.');
    }
  }
  useEffect(() => {
    alive.current = true;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      if (available) {
        try {
          await refresh();
        } catch (e) {
          if (!stopped) report(e);
        }
      }
      if (!stopped) {
        setNow(Date.now());
        timer = setTimeout(poll, 3000);
      }
    }
    void poll();
    return () => {
      stopped = true;
      alive.current = false;
      clearTimeout(timer);
    };
  }, [available]);
  useEffect(() => {
    setJobs([]);
    if (available) void refresh().catch(report);
  }, [selected]);
  async function perform(operation: () => Promise<void>) {
    if (locked.current) return;
    locked.current = true;
    setBusy(true);
    setError('');
    setNotice('');
    try {
      await operation();
    } catch (e) {
      report(e);
      throw e;
    } finally {
      locked.current = false;
      if (alive.current) setBusy(false);
    }
  }
  async function register() {
    await perform(async () => {
      const key = JSON.stringify([name.trim(), email.trim()]);
      if (pending.current?.key !== key) pending.current = { key, id: crypto.randomUUID() };
      const result = await api<{ environment: Environment; registration: Registration | null }>(
        '/onprem/environments',
        mutation(csrf, { request_id: pending.current.id, name: name.trim(), email: email.trim() }),
      );
      if (!alive.current) return;
      pending.current = null;
      setSelected(result.environment.id);
      activeId.current = result.environment.id;
      setRegistration(
        result.registration
          ? { ...result.registration, environmentId: result.environment.id }
          : null,
      );
      if (!result.registration) setNotice('등록된 환경을 찾았습니다. 준비 명령을 다시 발급하세요.');
      await refresh();
    });
  }
  async function reissue() {
    if (!row) return;
    const id = row.id;
    await perform(async () => {
      const result = await api<Registration>(
        `/onprem/environments/${id}/registration`,
        mutation(csrf),
      );
      if (alive.current) setRegistration({ ...result, environmentId: id });
      await refresh();
    });
  }
  async function job(action: 'check' | 'remove_environment') {
    if (!row) return;
    const key = `${row.id}:${action}`;
    if (pending.current?.key !== key) pending.current = { key, id: crypto.randomUUID() };
    await perform(async () => {
      await api(
        `/onprem/environments/${row.id}/jobs`,
        mutation(csrf, { request_id: pending.current!.id, action }),
      );
      pending.current = null;
      await refresh();
    });
  }

  return (
    <section className="onprem-page">
      <div className="page-heading">
        <div>
          <h1>온프레미스 환경</h1>
          <p>내 서버를 준비하고 연결을 확인합니다.</p>
        </div>
      </div>
      {!available ? (
        <p className="panel">서버 연결 기능이 준비되지 않았습니다.</p>
      ) : (
        <>
          {error && (
            <div className="error" role="alert">
              {error}
            </div>
          )}
          {notice && <p role="status">{notice}</p>}
          <form
            className="panel onprem-register"
            onSubmit={(event) => {
              event.preventDefault();
              void register().catch(() => {});
            }}
          >
            <h2>환경 등록</h2>
            <label>
              환경 이름
              <input
                required
                maxLength={100}
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            <label>
              인증서 안내 이메일
              <input
                required
                type="email"
                maxLength={254}
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
            </label>
            <button className="primary" disabled={busy}>
              등록하고 준비 명령 받기
            </button>
          </form>
          <div className="onprem-list" aria-label="온프레미스 환경 목록">
            {rows.map((item) => (
              <button
                className={selected === item.id ? 'secondary selected' : 'secondary'}
                key={item.id}
                onClick={() => {
                  setSelected(item.id);
                  setError('');
                }}
              >
                {item.name} · {states[item.status] ?? item.status}
              </button>
            ))}
          </div>
          {row && (
            <section className="panel">
              <h2>{row.name}</h2>
              <p role="status">
                {states[row.status] ?? row.status}
                {row.active_job_id ? ' · 작업 진행 중' : ''}
              </p>
              <h3>{row.guidance.title}</h3>
              <p>{row.guidance.message}</p>
              {setup && row.status === 'ISSUED' && (
                <div className="onprem-command">
                  <p>
                    이 명령은 이 화면에서만 표시됩니다. 공유하지 마세요. 만료:{' '}
                    {new Date(setup.expires_at).toLocaleString('ko-KR')}
                  </p>
                  {now < setup.expires_at ? (
                    <>
                      <pre>{setup.command}</pre>
                      <button
                        className="secondary"
                        onClick={() => {
                          void navigator.clipboard
                            .writeText(setup.command)
                            .then(() => setNotice('명령을 복사했습니다.'))
                            .catch(report);
                        }}
                      >
                        명령 복사
                      </button>
                    </>
                  ) : (
                    <p>명령이 만료되었습니다. 다시 발급하세요.</p>
                  )}
                </div>
              )}
              {row.public_ip && <p>확인한 공인 IP: {row.public_ip}</p>}
              {row.last_seen_at && (
                <p>마지막 확인: {new Date(row.last_seen_at).toLocaleString('ko-KR')}</p>
              )}
              {row.error_message && (
                <div className="error" role="alert">
                  {row.error_message}
                </div>
              )}
              <div className="mock-toolbar">
                <button
                  className="secondary"
                  disabled={waiting || row.status === 'ISSUED'}
                  onClick={() => {
                    void job('check').catch(() => {});
                  }}
                >
                  연결 다시 확인
                </button>
                <button
                  className="secondary"
                  disabled={waiting}
                  onClick={() => {
                    void reissue().catch(() => {});
                  }}
                >
                  준비 명령 재발급
                </button>
                <button
                  className="text-link"
                  disabled={busy}
                  onClick={() => {
                    void refresh().catch(report);
                  }}
                >
                  상태 새로고침
                </button>
              </div>
              <p className="quiet">
                배포 대상으로 선택한 환경은 준비 명령을 재발급할 수 없습니다. 앱을 먼저 삭제한 뒤
                환경을 정리하세요.
              </p>
              <DeleteRegistration
                label="환경 정리"
                name={row.name}
                disabled={waiting}
                description="남은 앱이 없는 환경의 DNS와 서비스 등록을 정리합니다. 서버의 Docker·deploy 계정·Traefik은 유지됩니다."
                onDelete={() => job('remove_environment')}
              />
              {jobs.map((item) => (
                <details key={item.id}>
                  <summary>
                    {actions[item.action] ?? item.action} · {item.status}
                  </summary>
                  {item.result.error && <p role="alert">{item.result.error.message}</p>}
                  <DeploymentLogs logs={item.logs} />
                </details>
              ))}
            </section>
          )}
        </>
      )}
    </section>
  );
}
