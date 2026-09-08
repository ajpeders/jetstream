export interface Viewer {
  ip: string;
  loc: string;
  last_seen: number;
  who: string | null;
}

export interface ViewerHistoryRow {
  ts?: number;
  ip?: string;
  loc?: string;
  who?: string;
}

export async function listViewers(): Promise<Viewer[] | undefined> {
  const r = await fetch('/admin/api/viewers');
  if (!r.ok) return undefined;
  const body = await r.json();
  return Array.isArray(body) ? body : [];
}

export async function viewerHistory(): Promise<ViewerHistoryRow[]> {
  const r = await fetch('/admin/api/viewers/history');
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const body = await r.json();
  return Array.isArray(body) ? body : [];
}

export interface ReportClient {
  title?: string;
  path?: string;
  position_seconds?: number | null;
  playback_mode?: string;
  muted?: boolean | null;
  fullscreen?: boolean | null;
}

export interface ReportStreamHealth {
  title?: string;
  encoder?: string;
  ffmpeg_alive?: boolean;
  viewers?: number | null;
  server_position_seconds?: number | null;
  segments_on_disk?: number | null;
  paused?: boolean;
}

export type ReportTriage =
  | string
  | {
      cause?: unknown;
      confidence?: unknown;
      reasoning?: unknown;
      suggested_action?: unknown;
    };

export interface Report {
  id: number;
  ts?: number;
  status?: string;
  message?: string;
  viewer_label?: string;
  ip?: string;
  user_agent?: string;
  client?: ReportClient;
  stream_health?: ReportStreamHealth;
  triage?: ReportTriage | null;
}

export async function listReports(): Promise<Report[] | undefined> {
  const r = await fetch('/admin/api/reports');
  if (!r.ok) return undefined;
  const body = await r.json();
  return Array.isArray(body) ? body : [];
}

export function setReportHandled(id: number, unhandle: boolean): Promise<Response> {
  return fetch(`/admin/api/reports/${id}/handled`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ unhandle }),
  });
}

export function removeReport(id: number): Promise<Response> {
  return fetch(`/admin/api/reports/${id}`, { method: 'DELETE' });
}

export function clearReports(): Promise<Response> {
  return fetch('/admin/api/reports', { method: 'DELETE' });
}

export interface QueueRequest {
  id: number;
  path: string | null;
  title: string | null;
  requester: string | null;
}

export async function listQueueRequests(): Promise<QueueRequest[] | undefined> {
  const r = await fetch('/admin/api/requests');
  if (!r.ok) return undefined;
  const body = await r.json();
  return Array.isArray(body) ? body : [];
}

export function approveQueueRequest(id: number): Promise<Response> {
  return fetch(`/admin/api/requests/${id}/approve`, { method: 'POST' });
}

export function denyQueueRequest(id: number): Promise<Response> {
  return fetch(`/admin/api/requests/${id}`, { method: 'DELETE' });
}

export function clearQueueRequests(): Promise<Response> {
  return fetch('/admin/api/requests', { method: 'DELETE' });
}

export interface MediaRequest {
  id: string;
  title: string | null;
  username: string | null;
  status?: 'pending' | 'approved' | 'rejected' | string;
  kind?: string | null;
  year?: number | string | null;
  tmdb_id?: number | null;
  tvdb_id?: number | null;
  seasons?: (number | string)[] | null;
  reject_reason?: string | null;
  requested_at?: number | null;
}

export async function listMediaRequests(): Promise<MediaRequest[] | undefined> {
  const r = await fetch('/admin/api/media/requests');
  if (!r.ok) return undefined;
  const body = await r.json();
  return Array.isArray(body) ? body : [];
}

export function approveMediaRequest(id: string): Promise<Response> {
  return fetch(`/admin/api/media/requests/${encodeURIComponent(id)}/approve`, { method: 'POST' });
}

export function rejectMediaRequest(id: string, reason: string): Promise<Response> {
  return fetch(`/admin/api/media/requests/${encodeURIComponent(id)}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  });
}

export interface CustomReaction {
  id: number;
  label: string | null;
  ip: string | null;
}

export async function listCustomReactions(): Promise<CustomReaction[] | undefined> {
  const r = await fetch('/admin/api/reactions');
  if (!r.ok) return undefined;
  const body = await r.json();
  return Array.isArray(body) ? body : [];
}

export function removeCustomReaction(id: number): Promise<Response> {
  return fetch(`/admin/api/reactions/${id}`, { method: 'DELETE' });
}

export interface UserAccount {
  id: string;
  username: string;
  created?: number | null;
  last_login?: number | null;
  disabled?: boolean;
  active_vod?: boolean;
  invited_by?: string | null;
}

export async function listUsers(): Promise<UserAccount[] | undefined> {
  const r = await fetch('/admin/api/users');
  if (!r.ok) return undefined;
  const body = await r.json();
  return Array.isArray(body.users) ? body.users : [];
}

export function createUser(username: string, password: string): Promise<Response> {
  return fetch('/admin/api/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
}

export function setUserPassword(id: string, password: string): Promise<Response> {
  return fetch(`/admin/api/users/${encodeURIComponent(id)}/password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  });
}

export function setUserDisabled(id: string, disabled: boolean): Promise<Response> {
  return fetch(`/admin/api/users/${encodeURIComponent(id)}/disabled`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ disabled }),
  });
}

export function removeUser(id: string): Promise<Response> {
  return fetch(`/admin/api/users/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export interface VodSession {
  session_id: string;
  username: string | null;
  title: string | null;
  start_offset: number | null;
  started_at: number | null;
  last_access: number | null;
}

export interface VodSessionFeed {
  sessions: VodSession[];
  cap: number | null;
}

export async function listVodSessions(): Promise<VodSessionFeed | undefined> {
  const r = await fetch('/admin/api/vod/sessions');
  if (!r.ok) return undefined;
  const body = await r.json();
  return {
    sessions: Array.isArray(body.sessions) ? body.sessions : [],
    cap: body.cap ?? null,
  };
}

export function killVodSession(sessionId: string): Promise<Response> {
  return fetch(`/admin/api/vod/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
}

export interface ChatMessage {
  id: number;
  sid: string;
  name?: string | null;
  text: string;
}

export interface ChatPage {
  messages?: ChatMessage[];
  deleted_ids?: number[];
  max_id?: number;
}

export async function recentChat(since: number): Promise<ChatPage | undefined> {
  const r = await fetch(`/chat/recent?since=${since}`);
  if (!r.ok) return undefined;
  return (await r.json()) as ChatPage;
}

export function sendChat(message: string, sid: string, name: string): Promise<Response> {
  return fetch('/chat/send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, sid, name }),
  });
}

export function deleteChatMessage(id: number): Promise<Response> {
  return fetch(`/admin/api/chat/${id}`, { method: 'DELETE' });
}

export function muteChatSid(sid: string, seconds: number): Promise<Response> {
  return fetch('/admin/api/chat/mute', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sid, seconds }),
  });
}

export interface QueueItem {
  type?: string;
  ref?: string;
  title?: string;
  subtitle_idx?: number | null;
  is_live?: boolean;
  duration?: number | null;
}

export interface RecentItem {
  source?: QueueItem;
  ts?: number | null;
}

export function adminApiBase(): string {
  return location.pathname.startsWith('/controls') ? '/api/control' : '/admin/api';
}
