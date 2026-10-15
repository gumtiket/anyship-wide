import { useId, useRef, useState } from 'react';
import { LoaderCircle, Trash2 } from 'lucide-react';
import './delete-registration.css';

export function DeleteRegistration({
  label,
  name,
  description,
  disabled = false,
  onDelete,
}: {
  label: string;
  name: string;
  description: string;
  disabled?: boolean;
  onDelete: () => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const inFlight = useRef(false);
  const titleId = useId(),
    descriptionId = useId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function remove() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError('');
    try {
      await onDelete();
      dialog.current?.close();
    } catch (e) {
      setError(e instanceof Error ? e.message : '삭제하지 못했습니다. 다시 시도해 주세요.');
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        className="secondary delete-registration"
        disabled={disabled || busy}
        onClick={() => {
          setError('');
          dialog.current?.showModal();
        }}
      >
        <Trash2 size={15} />
        {label}
      </button>
      <dialog
        ref={dialog}
        className="delete-dialog"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        onCancel={(event) => {
          if (inFlight.current) event.preventDefault();
        }}
      >
        <h2 id={titleId}>{label}하시겠어요?</h2>
        <p className="delete-target">{name}</p>
        <p id={descriptionId}>{description}</p>
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
            className="delete-confirm"
            disabled={busy}
            onClick={() => void remove()}
          >
            {busy ? <LoaderCircle size={15} className="spin" /> : <Trash2 size={15} />}{' '}
            {busy ? '삭제 중…' : label}
          </button>
        </div>
      </dialog>
    </>
  );
}
