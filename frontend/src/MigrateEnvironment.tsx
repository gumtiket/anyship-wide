import { useId, useRef, useState } from 'react';
import { ArrowRightLeft, LoaderCircle } from 'lucide-react';
import './delete-registration.css';
import './migrate-environment.css';

export type MigrationTarget = { id: string; name: string; region: string; set_name: string };

// 앱의 DB를 다른 환경으로 옮긴다. 시작하기 전에 무슨 일이 일어나는지와 옮겨지지 않는 것을 확인받는다.
export function MigrateEnvironment({
  currentName,
  candidates,
  secretCount,
  disabled = false,
  onMigrate,
}: {
  currentName: string;
  candidates: MigrationTarget[];
  secretCount: number;
  disabled?: boolean;
  onMigrate: (destination: MigrationTarget) => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const inFlight = useRef(false);
  const titleId = useId(),
    descriptionId = useId();
  const [destinationId, setDestinationId] = useState('');
  const [understood, setUnderstood] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const destination = candidates.find((item) => item.id === destinationId) ?? candidates[0];

  async function start() {
    if (inFlight.current || !destination) return;
    inFlight.current = true;
    setBusy(true);
    setError('');
    try {
      await onMigrate(destination);
      dialog.current?.close();
    } catch (e) {
      setError(
        e instanceof Error ? e.message : '환경 이전을 시작하지 못했습니다. 다시 시도해 주세요.',
      );
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  if (!candidates.length) {
    return (
      <p className="quiet migrate-none">
        옮길 수 있는 다른 환경이 없습니다.{' '}
        <a className="text-link" href="#aws">
          AWS 환경
        </a>{' '}
        또는{' '}
        <a className="text-link" href="#onprem">
          온프레미스 환경
        </a>
        을 먼저 연결하세요.
      </p>
    );
  }
  return (
    <div className="migrate-environment">
      <label>
        옮겨 갈 환경
        <select
          aria-label="옮겨 갈 환경"
          value={destination?.id ?? ''}
          disabled={disabled || busy}
          onChange={(event) => setDestinationId(event.target.value)}
        >
          {candidates.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name} · {item.region}
            </option>
          ))}
        </select>
      </label>
      <button
        type="button"
        className="secondary"
        disabled={disabled || busy || !destination}
        onClick={() => {
          setError('');
          setUnderstood(false);
          dialog.current?.showModal();
        }}
      >
        <ArrowRightLeft size={15} /> 다른 환경으로 옮기기
      </button>
      <dialog
        ref={dialog}
        className="delete-dialog migrate-dialog"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        onCancel={(event) => {
          if (inFlight.current) event.preventDefault();
        }}
      >
        <h2 id={titleId}>다른 환경으로 옮기시겠어요?</h2>
        <p className="delete-target">
          {currentName} → {destination?.name}
        </p>
        <div id={descriptionId}>
          <p>다음 순서로 진행합니다.</p>
          <ol>
            <li>지금 환경의 서버와 DB 크기를 먼저 확인합니다. DB가 너무 크면 시작하지 않습니다.</li>
            <li>
              선택한 환경에 이 프로젝트의 <strong>최신 소스</strong>를 새로 배포합니다. AWS 환경의
              첫 배포는 약 20분 걸릴 수 있습니다.
            </li>
            <li>
              지금 앱을 멈추고 DB를 복사한 뒤 테이블별 행 수를 비교합니다.{' '}
              <strong>이 동안 서비스가 잠시 멈춥니다.</strong>
            </li>
            <li>
              성공하면 이 프로젝트의 배포 환경이 새 환경으로 바뀝니다. 원래 앱은{' '}
              <strong>멈춘 채 남으니</strong> 새 주소에서 확인한 뒤 직접 지우세요.
            </li>
          </ol>
          <p className="migrate-limits">
            옮겨지는 것은 <strong>앱의 DB</strong>뿐입니다. 서버에 저장한 파일은 옮겨지지 않고, 앱의
            비밀 키는 환경마다 새로 만들어져 기존 로그인 세션이 풀립니다. 공개 주소가 바뀌며, 앱에
            필요한 비밀 값은 다시 보내야 합니다
            {secretCount > 0
              ? ` (입력한 ${secretCount}개를 함께 보냅니다)`
              : ' (입력한 값이 없습니다)'}
            . 중간에 실패해도 지금 환경의 앱은 다시 시작되지만, 새 환경에는 앱이 남아 있을 수
            있습니다.
          </p>
        </div>
        <label className="migrate-ack">
          <input
            type="checkbox"
            checked={understood}
            disabled={busy}
            onChange={(event) => setUnderstood(event.target.checked)}
          />
          <span>이전하시겠습니까?</span>
        </label>
        {error && (
          <div className="error" role="alert">
            {error}
          </div>
        )}
        <div className="delete-dialog-actions">
          <button
            type="button"
            className="secondary"
            autoFocus
            disabled={busy}
            onClick={() => dialog.current?.close()}
          >
            취소
          </button>
          <button
            type="button"
            className="primary"
            disabled={busy || !understood}
            onClick={() => void start()}
          >
            {busy ? <LoaderCircle size={15} className="spin" /> : <ArrowRightLeft size={15} />}{' '}
            {busy ? '시작하는 중…' : '옮기기 시작'}
          </button>
        </div>
      </dialog>
    </div>
  );
}
