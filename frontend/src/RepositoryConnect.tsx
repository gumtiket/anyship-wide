import { useEffect, useRef, useState } from 'react';
import {
  ArrowRight,
  Check,
  ExternalLink,
  GitBranch,
  Github,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react';
import { api, mutation, type ConnectionDraft, type Project } from './api';
import './onboarding.css';

type Repository = {
  id: number;
  full_name: string;
  private: boolean;
  default_branch: string;
  branches: { name: string; sha: string }[];
};
type Connection = ConnectionDraft & {
  status:
    | 'ready'
    | 'connected'
    | 'authorization_required'
    | 'permissions_required'
    | 'suspended'
    | 'archived'
    | 'write_required'
    | 'empty_repository';
  message?: string;
  repository?: Repository;
  project_id?: string;
};
const titles: Record<Connection['status'], string> = {
  ready: '저장소 접근을 확인했습니다',
  connected: '이미 연결한 프로젝트입니다',
  authorization_required: '저장소 접근을 허용해 주세요',
  permissions_required: '요청된 권한을 승인해 주세요',
  suspended: '저장소 연결이 일시 중지되었습니다',
  archived: '보관된 저장소입니다',
  write_required: '저장소 쓰기 권한이 필요합니다',
  empty_repository: '첫 커밋을 추가해 주세요',
};

export function RepositoryConnect({
  csrf,
  returnState,
  onReturnHandled,
  onConnected,
  onError,
}: {
  csrf: string;
  returnState: string | null;
  onReturnHandled: () => void;
  onConnected: (projectId: string) => Promise<void>;
  onError: (error: unknown) => void;
}) {
  const [url, setUrl] = useState('');
  const [draft, setDraft] = useState<ConnectionDraft | null>(null);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [branch, setBranch] = useState('');
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [resumed, setResumed] = useState(false);
  const mounted = useRef(true);
  const repo = connection?.repository;
  const accessConfirmed =
    connection && ['ready', 'connected', 'empty_repository'].includes(connection.status);
  const canAuthorize =
    connection?.status === 'authorization_required' ||
    connection?.status === 'permissions_required';

  function report(e: unknown) {
    if (!mounted.current) return;
    onError(e);
    setError(e instanceof Error ? e.message : '저장소 연결을 확인하지 못했습니다.');
  }
  function accept(result: Connection) {
    if (!mounted.current) return;
    setDraft(result);
    setConnection(result);
    setUrl(result.repository_url);
    const available = result.repository?.branches ?? [];
    setBranch((previous) =>
      available.some((b) => b.name === previous)
        ? previous
        : (available.find((b) => b.name === result.repository?.default_branch)?.name ??
          available[0]?.name ??
          ''),
    );
  }
  async function check(value: ConnectionDraft) {
    accept(
      await api<Connection>(
        '/github/connection/check',
        mutation(csrf, { connection_id: value.id }),
      ),
    );
  }
  useEffect(() => {
    mounted.current = true;
    onReturnHandled();
    (async () => {
      try {
        const saved = await api<ConnectionDraft | null>('/github/connection');
        if (!mounted.current) return;
        if (!saved) {
          if (returnState)
            setError('연결 요청이 만료되었습니다. 저장소 주소를 다시 입력해 주세요.');
          return;
        }
        setDraft(saved);
        setUrl(saved.repository_url);
        setResumed(true);
        if (returnState)
          accept(
            await api<Connection>(
              '/github/connection/return',
              mutation(csrf, { state: returnState }),
            ),
          );
        else await check(saved);
      } catch (e) {
        report(e);
      } finally {
        if (mounted.current) setBusy(false);
      }
    })();
    return () => {
      mounted.current = false;
    };
  }, []);

  async function resolve() {
    setBusy(true);
    setError('');
    setConnection(null);
    try {
      const saved = await api<ConnectionDraft>(
        '/github/connection',
        mutation(csrf, { repository_url: url.trim() }),
      );
      if (!mounted.current) return;
      setDraft(saved);
      setUrl(saved.repository_url);
      setResumed(false);
      await check(saved);
    } catch (e) {
      report(e);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  async function recheck() {
    if (!draft) return;
    setBusy(true);
    setError('');
    setConnection(null);
    try {
      await check(draft);
    } catch (e) {
      report(e);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  async function authorize() {
    if (!draft) return;
    setBusy(true);
    setError('');
    try {
      const result = await api<{ url: string }>(
        '/github/connection/authorize',
        mutation(csrf, { connection_id: draft.id }),
      );
      if (mounted.current) window.location.assign(result.url);
    } catch (e) {
      report(e);
      if (mounted.current) setBusy(false);
    }
  }
  async function connect() {
    if (!repo || !branch) return;
    setBusy(true);
    setError('');
    try {
      const project = await api<Project>(
        '/projects',
        mutation(csrf, { repository_url: `https://github.com/${repo.full_name}`, branch }),
      );
      if (mounted.current) await onConnected(project.id);
    } catch (e) {
      report(e);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  async function cancel() {
    if (!draft) return;
    setBusy(true);
    setError('');
    try {
      await api('/github/connection', mutation(csrf, { connection_id: draft.id }, 'DELETE'));
      if (!mounted.current) return;
      setDraft(null);
      setConnection(null);
      setUrl('');
      setBranch('');
      setResumed(false);
    } catch (e) {
      report(e);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">CONNECT YOUR REPOSITORY</span>
          <h1>첫 단계는 저장소 연결입니다.</h1>
          <p>GitHub 주소를 입력하면 필요한 접근 권한을 안내합니다.</p>
        </div>
      </div>
      <ol className="connection-steps" aria-label="저장소 연결 단계">
        <li className={!connection ? 'current' : 'done'}>
          <span>{connection ? <Check size={14} /> : '1'}</span> 주소 확인
        </li>
        <li className={accessConfirmed ? 'done' : connection ? 'current' : ''}>
          <span>{accessConfirmed ? <Check size={14} /> : '2'}</span> 접근 승인
        </li>
        <li
          className={
            connection?.status === 'connected'
              ? 'done'
              : connection?.status === 'ready'
                ? 'current'
                : ''
          }
        >
          <span>{connection?.status === 'connected' ? <Check size={14} /> : '3'}</span>{' '}
          {connection?.status === 'connected' ? '프로젝트 연결 완료' : '기준 브랜치 선택'}
        </li>
      </ol>
      {resumed && (
        <p className="resume-note" role="status">
          <RefreshCw size={15} /> 진행 중이던 저장소 연결을 이어갑니다.
        </p>
      )}
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      <div className="connect-grid">
        <section className="panel">
          <h2>GitHub 저장소</h2>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              resolve();
            }}
          >
            <label htmlFor="repository-url">저장소 URL</label>
            <input
              id="repository-url"
              type="url"
              placeholder="https://github.com/owner/repository"
              value={url}
              required
              disabled={busy}
              autoComplete="off"
              onChange={(e) => {
                setUrl(e.target.value);
                setConnection(null);
                setBranch('');
              }}
            />
            <p className="helper">
              공개·비공개 저장소를 연결할 수 있습니다. 코드를 수정할 권한이 있는 저장소를
              입력하세요.
            </p>
            <button className="primary" disabled={busy || !url.trim()} type="submit">
              {busy ? <LoaderCircle size={16} className="spin" /> : <ArrowRight size={16} />}{' '}
              {busy ? '확인하고 있습니다' : '저장소 확인'}
            </button>
          </form>
          {connection && (
            <div className="connection-result" role="status">
              <div className="connection-result-title">
                <ShieldCheck size={19} />
                <h3>{titles[connection.status]}</h3>
              </div>
              {connection.message && <p>{connection.message}</p>}
              {repo && (
                <p className="repository-name">
                  <strong>{repo.full_name}</strong>
                  <span>{repo.private ? '비공개' : '공개'} 저장소</span>
                </p>
              )}
              {canAuthorize && (
                <>
                  <button className="primary full" disabled={busy} onClick={authorize}>
                    <Github size={17} /> GitHub에서 접근 승인 <ExternalLink size={14} />
                  </button>
                  <p className="helper">
                    GitHub에서 연결할 계정과 이 저장소를 선택하세요. 승인 후 돌아오면 입력한 주소로
                    이어집니다.
                  </p>
                </>
              )}
              {connection.status === 'connected' && (
                <button
                  className="primary"
                  disabled={busy}
                  onClick={async () => {
                    setBusy(true);
                    try {
                      await api(
                        '/github/connection',
                        mutation(csrf, { connection_id: connection.id }, 'DELETE'),
                      );
                      await onConnected(connection.project_id!);
                    } catch (e) {
                      report(e);
                    } finally {
                      if (mounted.current) setBusy(false);
                    }
                  }}
                >
                  프로젝트 열기 <ArrowRight size={16} />
                </button>
              )}
            </div>
          )}
          {draft && (
            <div className="connection-actions">
              <button
                className="secondary"
                disabled={busy || url.trim() !== draft.repository_url}
                onClick={recheck}
              >
                <RefreshCw size={14} /> 접근 권한 다시 확인
              </button>
              <button className="text-link" disabled={busy} onClick={cancel}>
                입력 초기화
              </button>
            </div>
          )}
          {draft?.awaiting_approval && (
            <p className="helper">
              승인 후에도 이 화면이 그대로라면 접근 권한을 다시 확인하세요. 조직 관리자에게 승인을
              요청한 경우에는 승인 후 이어갈 수 있습니다.
            </p>
          )}
        </section>
        <section className="panel selection">
          <h2>{connection?.status === 'connected' ? '연결 완료' : '기준 브랜치'}</h2>
          {connection?.status === 'connected' ? (
            <div className="selection-empty">
              <Check size={28} />
              <p>
                기존 프로젝트에서 이어서 작업하세요.
                <br />
                분석 요청과 변경 검토를 진행할 수 있습니다.
              </p>
            </div>
          ) : connection?.status === 'ready' && repo ? (
            <>
              <label htmlFor="branch">변경을 시작할 브랜치</label>
              <select
                id="branch"
                value={branch}
                disabled={busy}
                onChange={(e) => setBranch(e.target.value)}
              >
                {repo.branches.map((item) => (
                  <option key={item.name}>{item.name}</option>
                ))}
              </select>
              <p className="helper">변경은 별도 브랜치에 작성하고, 검토 후 PR로 제출합니다.</p>
              <div className="commit-preview">
                <span>현재 커밋</span>
                <code>{repo.branches.find((b) => b.name === branch)?.sha.slice(0, 12)}</code>
              </div>
            </>
          ) : (
            <div className="selection-empty">
              <GitBranch size={28} />
              <p>
                저장소 접근을 확인하면
                <br />
                기준 브랜치를 선택할 수 있습니다.
              </p>
            </div>
          )}
          {connection?.status !== 'connected' && (
            <div className="selection-footer">
              <button
                className="primary full"
                disabled={connection?.status !== 'ready' || !branch || busy}
                onClick={connect}
              >
                프로젝트 연결 <ArrowRight size={16} />
              </button>
            </div>
          )}
        </section>
      </div>
      <div className="connection-privacy">
        <ShieldCheck size={18} />
        <p>
          선택한 저장소만 허용해도 사용할 수 있습니다.
          <br />
          저장소 연결만으로 코드를 수정하거나 PR을 만들지 않습니다.
        </p>
      </div>
    </>
  );
}
