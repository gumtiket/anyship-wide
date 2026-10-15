export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code?: string,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch('/api' + path, { credentials: 'same-origin', ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new ApiError(
      response.status,
      typeof data.detail === 'string'
        ? data.detail
        : typeof data.detail?.message === 'string'
          ? data.detail.message
          : Array.isArray(data.detail)
            ? data.detail
                .map((item: { msg?: string }) => item.msg)
                .filter(Boolean)
                .join(' · ')
            : '요청을 처리할 수 없습니다. 다시 시도해 주세요.',
      data.detail?.code,
    );
  return data as T;
}

export function mutation(csrf: string, body: unknown = {}, method = 'POST'): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
    body: JSON.stringify(body),
  };
}

export type Project = {
  id: string;
  full_name: string;
  branch: string;
  base_sha: string;
  created_at: number;
};
export type ConnectionDraft = { id: string; repository_url: string; awaiting_approval: boolean };
