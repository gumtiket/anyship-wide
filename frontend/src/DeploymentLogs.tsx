import { useLayoutEffect, useRef, useState } from 'react';
import { ArrowDown } from 'lucide-react';
import './deployment-logs.css';

export type LogEntry = {
  step: number;
  total: number;
  name: string;
  message: string;
  level: string;
};

export function DeploymentLogs({ logs }: { logs: LogEntry[] }) {
  const viewport = useRef<HTMLDivElement>(null);
  const [following, setFollowing] = useState(true);

  useLayoutEffect(() => {
    if (following && viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight;
  }, [logs, following]);

  if (!logs.length) return <p className="small quiet">아직 수신한 로그가 없습니다.</p>;

  return (
    <div className="deployment-logs">
      <div className="deployment-log-toolbar">
        <strong>
          진행 로그 <span>{logs.length}개</span>
        </strong>
        <button
          type="button"
          className="text-link"
          disabled={following}
          onClick={() => setFollowing(true)}
        >
          <ArrowDown size={14} /> 최신 로그 보기
        </button>
      </div>
      <div
        ref={viewport}
        className="deployment-log-scroll"
        role="region"
        aria-label="배포 진행 로그, 스크롤하여 확인"
        aria-live="off"
        tabIndex={0}
        onScroll={(event) => {
          const element = event.currentTarget;
          setFollowing(element.scrollHeight - element.scrollTop - element.clientHeight < 24);
        }}
      >
        <ol className="deployment-log-list">
          {logs.map((entry, index) => (
            <li
              key={index}
              className={
                entry.level === 'error' ? 'is-error' : entry.level === 'warn' ? 'is-warn' : ''
              }
            >
              <span className="deployment-log-step">
                {entry.total ? `${entry.step}/${entry.total}` : '·'}
              </span>
              <div>
                {entry.name && <strong>{entry.name}</strong>}
                {entry.level === 'error' && <span className="deployment-log-level">오류</span>}
                {entry.level === 'warn' && <span className="deployment-log-level">경고</span>}
                <p>{entry.message}</p>
              </div>
            </li>
          ))}
        </ol>
      </div>
      <p className="deployment-log-follow">
        {following
          ? '새 로그를 자동으로 표시합니다.'
          : '이전 로그를 확인 중입니다. 최신 로그 보기로 자동 표시를 다시 켤 수 있습니다.'}
      </p>
    </div>
  );
}
