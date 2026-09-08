export interface Me {
  id: string;
  username: string;
}

export interface NowPlaying {
  playing?: boolean;
  title?: string;
}

export async function me(): Promise<Me | 'unauthenticated' | undefined> {
  const r = await fetch('/api/auth/me');
  if (r.status === 401) return 'unauthenticated';
  if (!r.ok) return undefined;
  return (await r.json()) as Me;
}

export function logout(): Promise<Response> {
  return fetch('/api/auth/logout', { method: 'POST' });
}

export async function nowPlaying(): Promise<NowPlaying | undefined> {
  const r = await fetch('/api/now-playing');
  if (!r.ok) return undefined;
  return (await r.json()) as NowPlaying;
}

export function changePassword(currentPassword: string, newPassword: string): Promise<Response> {
  return fetch('/api/auth/password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  });
}
