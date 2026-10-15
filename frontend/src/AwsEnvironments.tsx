import { useEffect, useRef, useState } from 'react';
import {
  Check,
  Cloud,
  ExternalLink,
  LoaderCircle,
  Plus,
  RefreshCw,
  Save,
  ShieldCheck,
} from 'lucide-react';
import { api, mutation } from './api';
import { DeleteRegistration } from './DeleteRegistration';
import './aws.css';

type Environment = {
  id: string;
  name: string;
  region: string;
  request_id: string;
  status: 'PENDING' | 'VERIFYING' | 'CONNECTED' | 'FAILED' | 'EXPIRED';
  cloudformation_url: string | null;
  role_arn: string | null;
  aws_account_id: string | null;
  submitted_role_arn: string | null;
  error_code: string | null;
  expires_at: number;
  verified_at: number | null;
};
type Draft = { request_id: string; name: string; region: string };
type AwsConfig = {
  aws_available: boolean;
  aws_verification_available: boolean;
  aws_regions: string[];
  aws_setup_issues?: string[];
};
type Props = {
  csrf: string;
  workspaceKey: string;
  environmentId: string | null;
  onSelect: (id: string | null) => void;
  onError: (error: unknown) => void;
};
const setupLabels: Record<string, string> = {
  template: 'AWS 역할 생성 템플릿',
  service_role: '서비스의 AWS 접근 역할',
  regions: '지원 리전',
  live_mode: '실제 AWS 연결 모드',
};
const labels: Record<Environment['status'], string> = {
  PENDING: '역할 생성 대기',
  VERIFYING: '검증 중',
  CONNECTED: '연결 완료',
  FAILED: '검증 실패',
  EXPIRED: '등록 만료',
};
function statusLabel(row: Environment, verificationAvailable: boolean) {
  if (
    !verificationAvailable &&
    row.submitted_role_arn &&
    ['PENDING', 'FAILED'].includes(row.status)
  )
    return 'ARN 저장 · 검증 대기';
  if (row.status === 'PENDING' && row.submitted_role_arn) return 'ARN 저장 · 검증 대기';
  if (row.status === 'FAILED' && row.error_code === 'service_credentials_unavailable')
    return '서버 인증 대기';
  return labels[row.status];
}
function readDraft(key: string): Draft | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || 'null');
    return value &&
      typeof value.name === 'string' &&
      typeof value.region === 'string' &&
      typeof value.request_id === 'string'
      ? value
      : null;
  } catch {
    return null;
  }
}
function cloudFormationUrl(value: string | null) {
  try {
    const url = new URL(value || '');
    return url.protocol === 'https:' && /^[a-z0-9-]+\.console\.aws\.amazon\.com$/.test(url.hostname)
      ? url.href
      : null;
  } catch {
    return null;
  }
}

export function AwsEnvironments({ csrf, workspaceKey, environmentId, onSelect, onError }: Props) {
  const storageKey = `anyship.aws.draft:${workspaceKey}`;
  const [configuration, setConfiguration] = useState<AwsConfig | null>(null);
  const available = configuration?.aws_available ?? false,
    regions = configuration?.aws_regions ?? [];
  const verificationAvailable = configuration?.aws_verification_available ?? false;
  const [draft, setDraft] = useState<Draft | null>(() => readDraft(storageKey));
  const [name, setName] = useState(() => readDraft(storageKey)?.name || '');
  const [region, setRegion] = useState(() => readDraft(storageKey)?.region || regions[0] || '');
  const [rows, setRows] = useState<Environment[]>([]);
  const [selected, setSelected] = useState<Environment | null>(null);
  const [roleArn, setRoleArn] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const mounted = useRef(true),
    locked = useRef(false),
    revision = useRef(0);
  const activeId = useRef(environmentId);
  activeId.current = environmentId;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      revision.current++;
    };
  }, []);
  function report(e: unknown) {
    if (!mounted.current) return;
    onError(e);
    setError(e instanceof Error ? e.message : 'AWS 연결을 확인하지 못했습니다.');
  }
  function accept(row: Environment, replaceRole = true) {
    setSelected(row);
    setName(row.name);
    setRegion(row.region);
    if (replaceRole || row.role_arn) setRoleArn(row.role_arn || row.submitted_role_arn || '');
  }
  async function load(id: string | null) {
    const version = ++revision.current;
    setLoading(true);
    try {
      const [config, items, row] = await Promise.all([
        api<AwsConfig>('/config'),
        api<Environment[]>('/aws/environments?limit=20'),
        id
          ? api<Environment>(`/aws/environments/${encodeURIComponent(id)}`)
          : Promise.resolve(null),
      ]);
      if (!mounted.current || version !== revision.current) return;
      setConfiguration(config);
      setRows(items);
      setHasMore(items.length === 20);
      if (row) accept(row, selected?.id !== row.id);
      else {
        setSelected(null);
        if (!draft)
          setRegion((value) =>
            config.aws_regions.includes(value) ? value : config.aws_regions[0] || '',
          );
      }
    } catch (e) {
      if (version === revision.current) report(e);
    } finally {
      if (mounted.current && version === revision.current) setLoading(false);
    }
  }
  useEffect(() => {
    setError('');
    setNotice('');
    setSelected(null);
    setRoleArn('');
    if (!environmentId) {
      const pending = readDraft(storageKey);
      setDraft(pending);
      setName(pending?.name || '');
      setRegion(pending?.region || regions[0] || '');
    }
    void load(environmentId);
  }, [environmentId]);

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
    } finally {
      locked.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  async function register() {
    await perform(async () => {
      const body = draft || { request_id: crypto.randomUUID(), name: name.trim(), region };
      if (!body.name || !body.region) throw new Error('환경 이름과 리전을 입력해 주세요.');
      // 요청 전에 저장해 두어, 응답이 유실돼도 환경을 중복 생성하지 않고 다시 시도할 수 있게 한다.
      sessionStorage.setItem(storageKey, JSON.stringify(body));
      setDraft(body);
      const row = await api<Environment>('/aws/environments', mutation(csrf, body));
      if (!mounted.current) return;
      sessionStorage.removeItem(storageKey);
      setDraft(null);
      accept(row);
      onSelect(row.id);
    });
  }
  async function saveRole() {
    if (!selected) return;
    const id = selected.id;
    await perform(async () => {
      const row = await api<Environment>(
        `/aws/environments/${id}/role`,
        mutation(csrf, { role_arn: roleArn.trim() }),
      );
      if (!mounted.current || activeId.current !== id) return;
      accept(row);
      setRows((items) => items.map((item) => (item.id === row.id ? row : item)));
      setNotice(
        verificationAvailable
          ? 'Role ARN을 저장했습니다. 연결 확인을 진행해 주세요.'
          : 'Role ARN을 저장했습니다. 연결 확인 기능이 준비되면 검증할 수 있습니다.',
      );
    });
  }
  async function verify() {
    if (!selected || !verificationAvailable) return;
    const id = selected.id;
    await perform(async () => {
      try {
        const row = await api<Environment>(
          `/aws/environments/${id}/verify`,
          mutation(csrf, { role_arn: roleArn.trim() }),
        );
        if (!mounted.current || activeId.current !== id) return;
        accept(row);
        setRows((items) => items.map((item) => (item.id === row.id ? row : item)));
        setNotice('AWS 계정이 연결됐습니다. 검증한 역할과 계정 정보가 저장됐습니다.');
      } catch (e) {
        try {
          const row = await api<Environment>(`/aws/environments/${id}`);
          if (mounted.current && activeId.current === id) {
            accept(row, false);
            setRows((items) => items.map((item) => (item.id === row.id ? row : item)));
            if (row.submitted_role_arn === roleArn.trim() && row.status !== 'CONNECTED') {
              setNotice('Role ARN은 저장돼 있습니다. 연결 검증은 완료되지 않았습니다.');
            }
          }
        } catch {
          /* 확인 오류를 그대로 둔다. */
        }
        throw e;
      }
    });
  }
  async function more() {
    await perform(async () => {
      const items = await api<Environment[]>(`/aws/environments?limit=20&offset=${rows.length}`);
      if (mounted.current) {
        setRows((previous) => [...previous, ...items]);
        setHasMore(items.length === 20);
      }
    });
  }
  async function removeEnvironment() {
    if (!selected || locked.current) throw new Error('현재 작업을 마친 뒤 다시 시도해 주세요.');
    const id = selected.id;
    locked.current = true;
    setBusy(true);
    setError('');
    try {
      await api(`/aws/environments/${id}`, mutation(csrf, {}, 'DELETE'));
      if (!mounted.current) return;
      setRows((items) => items.filter((item) => item.id !== id));
      if (activeId.current === id) {
        revision.current++;
        sessionStorage.removeItem(storageKey);
        setDraft(null);
        setSelected(null);
        setRoleArn('');
        setName('');
        setRegion(regions[0] || '');
        onSelect(null);
      }
    } catch (e) {
      onError(e);
      throw e;
    } finally {
      locked.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  function startNew() {
    if (locked.current) return;
    sessionStorage.removeItem(storageKey);
    setDraft(null);
    setSelected(null);
    setRoleArn('');
    setName('');
    setRegion(regions[0] || '');
    setError('');
    setNotice('');
    onSelect(null);
  }
  const ready = !loading && !busy,
    connected = selected?.status === 'CONNECTED';
  const url = cloudFormationUrl(selected?.cloudformation_url || null);
  const canSave = ready && selected && ['PENDING', 'FAILED'].includes(selected.status);
  const canVerify = available && verificationAvailable && canSave;
  const savedRoleArn = selected?.role_arn || selected?.submitted_role_arn;
  const supportedRegions = [
    ...new Set([...regions, ...(selected ? [selected.region] : draft ? [draft.region] : [])]),
  ];
  const setupIssues = (configuration?.aws_setup_issues ?? []).filter((issue) => setupLabels[issue]);
  function refresh() {
    setError('');
    setNotice('');
    void load(environmentId);
  }

  return (
    <div className="aws-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">CLOUD ENVIRONMENTS</span>
          <h1>AWS 환경</h1>
          <p>내 AWS 계정의 역할을 연결하고 연결 상태를 확인하세요.</p>
        </div>
        <button className="secondary" disabled={!ready} onClick={refresh}>
          <RefreshCw size={15} /> 상태 새로고침
        </button>
      </div>
      {configuration && !available && (
        <div className="aws-setup" role="status">
          <Cloud size={20} />
          <div>
            <strong>서비스 운영자의 AWS 설정이 필요합니다</strong>
            <p>
              환경 이름과 리전은 미리 입력할 수 있습니다. 역할 생성 링크를 받으려면 서비스 운영자가
              연결 설정을 완료해야 합니다.
            </p>
            {setupIssues.length > 0 && (
              <ul className="aws-setup-list" aria-label="미완료 서비스 설정">
                {setupIssues.map((issue) => (
                  <li key={issue}>
                    {setupLabels[issue]} <span>미설정</span>
                  </li>
                ))}
              </ul>
            )}
            <p>
              이 설정은 서비스 서버에 적용합니다. 이용자는 AWS에서 역할을 만든 뒤 아래에 Role ARN을
              입력하면 됩니다.
            </p>
            <button className="secondary" disabled={!ready} onClick={refresh}>
              <RefreshCw size={14} /> 설정 다시 확인
            </button>
          </div>
        </div>
      )}
      {configuration && available && !verificationAvailable && (
        <div className="aws-setup" role="status">
          <Cloud size={20} />
          <div>
            <strong>연결 확인 기능을 준비 중입니다</strong>
            <p>
              환경 등록, AWS 역할 생성, Role ARN 저장까지 진행할 수 있습니다. 실제 AWS 연결 검증은
              기능이 준비된 뒤 진행합니다.
            </p>
          </div>
        </div>
      )}
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <p className="resume-note" role="status">
          <Check size={16} />
          {notice}
        </p>
      )}
      <ol className="connection-steps" aria-label="AWS 환경 연결 단계">
        <li className={selected ? 'done' : 'current'}>
          <span>{selected ? <Check size={14} /> : '1'}</span> 환경 등록
        </li>
        <li className={savedRoleArn ? 'done' : selected ? 'current' : ''}>
          <span>{savedRoleArn ? <Check size={14} /> : '2'}</span> 역할 생성 · ARN 저장
        </li>
        <li className={connected ? 'done' : savedRoleArn ? 'current' : ''}>
          <span>{connected ? <Check size={14} /> : '3'}</span> 연결 확인
        </li>
      </ol>
      <div className="aws-grid">
        <div className="aws-flow">
          <section className="panel">
            <h2>{selected ? selected.name : '새 AWS 환경'}</h2>
            {loading && (
              <p role="status">
                <LoaderCircle size={15} className="spin" /> 연결 정보를 불러오고 있습니다.
              </p>
            )}
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void register();
              }}
            >
              <label htmlFor="aws-name">환경 이름</label>
              <input
                id="aws-name"
                value={name}
                maxLength={100}
                required
                placeholder="예: 개인 AWS 테스트"
                disabled={!ready || !!selected || !!draft}
                onChange={(e) => setName(e.target.value)}
              />
              <label htmlFor="aws-region">리전</label>
              <select
                id="aws-region"
                value={region}
                required
                disabled={!ready || !!selected || !!draft || !supportedRegions.length}
                onChange={(e) => setRegion(e.target.value)}
              >
                {!supportedRegions.length && (
                  <option value="">서비스에 지원 리전이 설정되지 않았습니다</option>
                )}
                {supportedRegions.map((value) => (
                  <option key={value} value={value}>
                    {value === 'ap-northeast-2' ? '서울 · ap-northeast-2' : value}
                  </option>
                ))}
              </select>
              {!selected && (
                <button
                  type="submit"
                  className="primary"
                  disabled={!available || !ready || !name.trim() || !region}
                >
                  {busy ? <LoaderCircle size={16} className="spin" /> : <Plus size={16} />}{' '}
                  {!configuration
                    ? '설정 확인 중'
                    : !available
                      ? '서비스 설정 필요'
                      : draft
                        ? '등록 요청 다시 확인'
                        : '등록 시작'}
                </button>
              )}
            </form>
            {draft && !selected && (
              <p className="helper">
                이전 등록 요청을 이어서 확인합니다. 같은 요청을 다시 보내도 중복 등록되지 않습니다.
              </p>
            )}
            {(selected || draft) && (
              <button className="text-link aws-new" disabled={!ready} onClick={startNew}>
                <Plus size={14} /> 새 환경 등록하기
              </button>
            )}
          </section>
          <section className="panel">
            <h2>AWS에서 역할 생성</h2>
            {url ? (
              <>
                <p>새 탭에서 본인의 AWS 계정에 로그인한 뒤 IAM 생성에 동의하고 스택을 만드세요.</p>
                <a className="primary" href={url} target="_blank" rel="noopener noreferrer">
                  AWS에서 역할 생성 <ExternalLink size={15} />
                </a>
                <p className="helper">
                  스택이 CREATE_COMPLETE가 되면 출력(Outputs)의 RoleArn을 복사해 아래에 입력하세요.
                </p>
                <p className="aws-permission">
                  <ShieldCheck size={15} /> 현재 온보딩 템플릿은 배포 역할에 관리자
                  권한(AdministratorAccess)을 부여합니다. AWS 화면에서 내용을 확인한 뒤 승인하세요.
                </p>
              </>
            ) : (
              <p className="helper">
                {connected
                  ? 'AWS 역할 연결이 완료됐습니다.'
                  : selected?.status === 'EXPIRED'
                    ? '등록이 만료됐습니다. 새 환경을 등록해 주세요.'
                    : selected?.status === 'VERIFYING'
                      ? '역할을 검증하고 있습니다. 잠시 후 상태를 새로고침하세요.'
                      : '환경 등록 후 역할 생성 링크가 표시됩니다.'}
              </p>
            )}
          </section>
          <section className="panel">
            <h2>Role ARN 저장 및 연결 확인</h2>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void saveRole();
              }}
            >
              <label htmlFor="aws-role-arn">AWS 출력의 RoleArn</label>
              <input
                id="aws-role-arn"
                value={roleArn}
                onChange={(e) => setRoleArn(e.target.value)}
                disabled={!canSave}
                required
                autoComplete="off"
                spellCheck={false}
                placeholder="arn:aws:iam::123456789012:role/deploy-service-role"
              />
              <p className="helper">
                ARN을 먼저 저장할 수 있습니다. 연결 확인은 입력값을 저장한 뒤 External ID로
                검증하며, 검증이 실패해도 저장한 ARN은 유지됩니다.
              </p>
              <div className="aws-role-actions">
                <button className="primary" disabled={!canSave || !roleArn.trim()} type="submit">
                  <Save size={16} /> Role ARN 저장
                </button>
                <button
                  className="secondary"
                  disabled={!canVerify || !roleArn.trim()}
                  type="button"
                  onClick={() => void verify()}
                >
                  <ShieldCheck size={16} />{' '}
                  {configuration && !verificationAvailable ? '연결 확인 준비 중' : '연결 확인'}
                </button>
                {busy && (
                  <span role="status">
                    <LoaderCircle size={15} className="spin" /> 처리하고 있습니다.
                  </span>
                )}
              </div>
            </form>
          </section>
        </div>
        <aside className="aws-summary">
          <section className="panel">
            <h2>현재 연결 상태</h2>
            <span className={`status aws-status ${connected ? 'is-connected' : ''}`}>
              {selected ? statusLabel(selected, verificationAvailable) : '미등록'}
            </span>
            <dl>
              <dt>환경</dt>
              <dd>{selected?.name || '—'}</dd>
              <dt>검증된 AWS 계정 ID</dt>
              <dd>{selected?.aws_account_id || '—'}</dd>
              <dt>저장된 Role ARN</dt>
              <dd>{savedRoleArn || '—'}</dd>
            </dl>
            {savedRoleArn && !connected && (
              <p className="helper">
                ARN은 저장됐으며 연결 검증은 완료되지 않았습니다. 새로고침하거나 다시 로그인해도
                저장한 값을 불러옵니다.
              </p>
            )}
            {selected?.status === 'FAILED' && verificationAvailable && (
              <p className="helper">
                {selected.error_code === 'service_credentials_unavailable'
                  ? '서비스 서버의 AWS 인증이 준비되면 같은 환경에서 연결 확인을 다시 눌러 주세요.'
                  : '권한과 입력한 ARN을 확인한 뒤 같은 환경에서 다시 시도할 수 있습니다.'}
              </p>
            )}
            {selected?.status === 'VERIFYING' && (
              <p className="helper">검증 중입니다. 잠시 후 상태 새로고침을 눌러 주세요.</p>
            )}
            {selected?.status === 'EXPIRED' && (
              <p className="helper">
                24시간이 지나 등록이 만료됐습니다. 새 환경으로 등록해 주세요.
              </p>
            )}
            {connected && <p className="helper">검증한 연결 정보가 저장돼 있습니다.</p>}
            {selected && (
              <div className="aws-delete">
                <DeleteRegistration
                  key={selected.id}
                  label="환경 삭제"
                  name={selected.name}
                  description="AnyShip에 저장된 환경과 연결 정보를 삭제합니다. AWS의 IAM 역할과 CloudFormation 스택은 그대로 유지됩니다."
                  disabled={!ready || selected.status === 'VERIFYING'}
                  onDelete={removeEnvironment}
                />
              </div>
            )}
          </section>
          <section className="panel">
            <h2>등록한 환경</h2>
            {!rows.length && (
              <p className="helper">{loading ? '불러오는 중…' : '아직 등록한 환경이 없습니다.'}</p>
            )}
            <ul className="aws-environment-list">
              {rows.map((row) => (
                <li key={row.id}>
                  <button
                    disabled={!ready}
                    className={selected?.id === row.id ? 'selected' : ''}
                    onClick={() => onSelect(row.id)}
                  >
                    <span>
                      <Cloud size={15} />
                      {row.name}
                    </span>
                    <small>{statusLabel(row, verificationAvailable)}</small>
                  </button>
                </li>
              ))}
            </ul>
            {hasMore && (
              <button className="text-link aws-new" disabled={!ready} onClick={() => void more()}>
                더 보기
              </button>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}
