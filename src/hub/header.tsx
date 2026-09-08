import { useEffect, useState } from 'react';

import { logout, me } from './api';

export function HubHeader() {
  const [who, setWho] = useState('');

  useEffect(() => {
    let alive = true;
    void me().then((result) => {
      if (!alive) return;
      if (result === 'unauthenticated') {
        location.href = '/login?next=%2Fhome';
        return;
      }
      if (result?.username) setWho(`Signed in as ${result.username}`);
    });
    return () => {
      alive = false;
    };
  }, []);

  const signOut = async () => {
    try {
      await logout();
    } catch {
      /* best effort */
    }
    location.href = '/';
  };

  return (
    <>
      <h1>jetstream</h1>
      <span id="who">{who}</span>
      <button className="du-btn du-btn-sm" id="logout-btn" type="button" onClick={signOut}>
        Sign out
      </button>
    </>
  );
}
