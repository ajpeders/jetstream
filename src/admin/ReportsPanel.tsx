import { useMemo } from 'react';

import { fmtAgo, formatTime, useActions, usePolled } from '@/lib';
import {
  clearReports,
  listReports,
  removeReport,
  setReportHandled,
  type Report,
  type ReportTriage,
} from './services';

interface MetaBit {
  k: string;
  v: string;
  mono?: boolean;
}

export function ReportsPanel() {
  const act = useActions();
  const poll = usePolled<Report[]>(listReports, { initial: [] });
  const reports = poll.data;
  const now = useMemo(() => Date.now() / 1000, [reports]);
  const unhandled = reports.filter((r) => r.status === 'new').length;

  const toggleHandled = async (r: Report) => {
    const unhandle = r.status === 'handled';
    await act.run(`handle:${r.id}`, () => setReportHandled(r.id, unhandle), {
      ok: unhandle ? 'Report reopened' : 'Marked handled',
      fail: 'Update failed',
    });
    poll.refresh();
  };
  const remove = async (r: Report) => {
    await act.run(`delete:${r.id}`, () => removeReport(r.id), {
      ok: 'Report deleted',
      fail: 'Delete failed',
    });
    poll.refresh();
  };
  const clearAll = async () => {
    if (!confirm("Delete ALL issue reports? This can't be undone.")) return;
    await act.run('clear', clearReports, {
      ok: (body) => {
        const n = (body as { cleared?: number }).cleared;
        return `Cleared ${n ?? ''} report${n === 1 ? '' : 's'}`.trim();
      },
      fail: 'Clear failed',
    });
    poll.refresh();
  };

  return (
    <>
      <div className="head">
        <h2>Reports</h2>
        <span className={`badge${unhandled > 0 ? ' waiting' : ''}`}>{unhandled}</span>
        {reports.length > 0 && (
          <button id="reports-clear" type="button" disabled={act.busy('clear')} onClick={clearAll} style={clearButtonStyle}>
            Clear all
          </button>
        )}
      </div>
      <ul>
        {reports.length ? reports.map((r) => <ReportRow key={r.id} r={r} now={now} busy={act.busy} toggleHandled={toggleHandled} remove={remove} />) : <li className="empty-r">No issue reports.</li>}
      </ul>
    </>
  );
}

function ReportRow({ r, now, busy, toggleHandled, remove }: {
  r: Report;
  now: number;
  busy(key: string): boolean;
  toggleHandled(r: Report): void;
  remove(r: Report): void;
}) {
  const handled = r.status === 'handled';
  return (
    <li className={handled ? 'handled' : undefined}>
      <div className="rtop">
        <span className={`rstatus ${handled ? 'handled' : 'new'}`}>{handled ? 'handled' : 'new'}</span>
        <span className="rwho">{r.viewer_label || 'anonymous'}</span>
        <span className="rtime">{r.ts ? fmtAgo(now - r.ts) : ''}</span>
      </div>
      <div className={`rmsg${r.message ? '' : ' none'}`}>{r.message || '(no message)'}</div>
      <div className="rmeta">
        {metaGroups(r).map((group, gi) => (
          <div key={gi}>
            {group.map((bit, bi) => (
              <span key={`${bit.k}:${bi}`}>
                <span className="k">{bit.k}</span>{' '}
                {bit.mono ? <code>{bit.v}</code> : bit.v}
                {bi < group.length - 1 && <span className="sep">&nbsp;·&nbsp;</span>}
              </span>
            ))}
          </div>
        ))}
      </div>
      {r.triage != null && <Triage triage={r.triage} />}
      <div className="ractions">
        <button type="button" className="handle" disabled={busy(`handle:${r.id}`)} onClick={() => toggleHandled(r)}>
          {handled ? 'Reopen' : 'Mark handled'}
        </button>
        <button type="button" disabled={busy(`delete:${r.id}`)} onClick={() => remove(r)}>
          Delete
        </button>
      </div>
    </li>
  );
}

function Triage({ triage }: { triage: ReportTriage }) {
  if (typeof triage === 'string') return <div className="rtriage"><span className="rtkey">triage</span> {triage}</div>;
  if (triage == null || typeof triage !== 'object') return <div className="rtriage"><span className="rtkey">triage</span> {String(triage)}</div>;
  const cause = triage.cause == null ? '' : String(triage.cause);
  const confidence = triage.confidence == null ? '' : String(triage.confidence);
  const reasoning = triage.reasoning == null ? '' : String(triage.reasoning);
  const suggested = triage.suggested_action == null ? '' : String(triage.suggested_action);
  const empty = !cause && !confidence && !reasoning && !suggested;
  return (
    <div className="rtriage">
      {cause && <><span className="rtkey">cause</span> <strong>{cause}</strong> </>}
      {confidence && <><span className="rtkey">confidence</span> {confidence}</>}
      {reasoning && <div>{reasoning}</div>}
      {suggested && <div><span className="rtkey">action</span> {suggested}</div>}
      {empty && <code>{JSON.stringify(triage)}</code>}
    </div>
  );
}

function metaGroups(r: Report): MetaBit[][] {
  const c = r.client ?? {};
  const h = r.stream_health ?? {};
  const client: MetaBit[] = [];
  const title = c.title || h.title;
  if (title) client.push({ k: 'title', v: title });
  if (c.path) client.push({ k: 'path', v: c.path, mono: true });
  if (c.position_seconds != null) client.push({ k: 'at', v: formatTime(c.position_seconds) });
  if (c.playback_mode) client.push({ k: 'mode', v: c.playback_mode });
  if (c.muted != null) client.push({ k: 'muted', v: c.muted ? 'yes' : 'no' });
  if (c.fullscreen != null) client.push({ k: 'fs', v: c.fullscreen ? 'yes' : 'no' });
  const health: MetaBit[] = [];
  if (h.encoder) health.push({ k: 'enc', v: h.encoder });
  health.push({ k: 'ffmpeg', v: h.ffmpeg_alive ? 'alive' : 'down' });
  if (h.viewers != null) health.push({ k: 'viewers', v: String(h.viewers) });
  if (h.server_position_seconds != null) health.push({ k: 'srv@', v: formatTime(h.server_position_seconds) });
  if (h.segments_on_disk != null) health.push({ k: 'segs', v: String(h.segments_on_disk) });
  if (h.paused) health.push({ k: 'paused', v: '' });
  const who: MetaBit[] = [];
  if (r.ip) who.push({ k: 'ip', v: r.ip, mono: true });
  if (r.user_agent) who.push({ k: 'ua', v: r.user_agent });
  return [client, health, who].filter((g) => g.length > 0);
}

const clearButtonStyle = { marginLeft: 'auto', fontSize: '0.75rem', padding: '0.2rem 0.55rem' };
