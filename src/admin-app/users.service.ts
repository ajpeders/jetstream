import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { EMPTY as NO_EMIT, Observable } from 'rxjs';
import { catchError, map } from 'rxjs/operators';

/** An admin-created account for private VOD streams. `invited_by` is null for
 *  host-created accounts and carries the invite code for self-registered ones. */
export interface UserAccount {
  id: string;
  username: string;
  created?: number | null;
  last_login?: number | null;
  disabled?: boolean;
  active_vod?: boolean;
  invited_by?: string | null;
}

@Injectable({ providedIn: 'root' })
export class UsersService {
  private readonly http = inject(HttpClient);

  list(): Observable<UserAccount[]> {
    return this.http.get<{ users?: UserAccount[] }>('/admin/api/users').pipe(
      map((r) => (Array.isArray(r.users) ? r.users : [])),
      catchError(() => NO_EMIT),
    );
  }

  /* Writes return Response so they compose with the shared actions() helper.
   * Create is the exception — the form needs the parsed error body to tell a
   * duplicate username apart from a server fault, so it is handled in the
   * component rather than through actions(). */

  create(username: string, password: string): Promise<Response> {
    return fetch('/admin/api/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
  }

  setPassword(id: string, password: string): Promise<Response> {
    return fetch(`/admin/api/users/${encodeURIComponent(id)}/password`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    });
  }

  setDisabled(id: string, disabled: boolean): Promise<Response> {
    return fetch(`/admin/api/users/${encodeURIComponent(id)}/disabled`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ disabled }),
    });
  }

  remove(id: string): Promise<Response> {
    return fetch(`/admin/api/users/${encodeURIComponent(id)}`, { method: 'DELETE' });
  }
}
