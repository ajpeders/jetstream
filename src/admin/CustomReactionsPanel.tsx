import { useActions, usePolled } from '@/lib';
import { listCustomReactions, removeCustomReaction, type CustomReaction } from './services';

export function CustomReactionsPanel() {
  const act = useActions();
  const poll = usePolled<CustomReaction[]>(listCustomReactions, { initial: [], intervalMs: 10000 });
  const items = poll.data;

  const remove = async (c: CustomReaction) => {
    await act.run(`del:${c.id}`, () => removeCustomReaction(c.id), {
      ok: 'Reaction removed',
      fail: 'Remove failed',
    });
    poll.refresh();
  };

  return (
    <>
      <div className="head">
        <h2>Custom reactions</h2>
        <span className="badge">{items.length}</span>
      </div>
      <div id="reactions-grid">
        {items.length ? items.map((c) => (
          <div className="rx" title={`${c.label ?? ''}${c.ip ? ` · ${c.ip}` : ''}`} key={c.id}>
            <img src={`/reactions/img/${c.id}`} alt="" />
            <button type="button" title="Delete" disabled={act.busy(`del:${c.id}`)} onClick={() => remove(c)}>✕</button>
          </div>
        )) : <span className="empty-r">No custom reactions uploaded.</span>}
      </div>
    </>
  );
}
