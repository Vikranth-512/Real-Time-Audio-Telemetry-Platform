import React, { useState, useEffect, useCallback } from 'react';

/** History icon (clock) for opening sidebar */
const HistoryIcon = ({ onClick, isOpen }) => (
  <button
    type="button"
    className="session-sidebar__trigger"
    onClick={onClick}
    aria-label={isOpen ? 'Close session history' : 'Open session history'}
    title="Session history"
  >
    <svg
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <circle cx="12" cy="12" r="10" />
      <polyline points="12 6 12 12 16 14" />
    </svg>
  </button>
);

/**
 * Sidebar drawer: session history with search.
 * Search by session_id via GET /api/sessions/search?q=
 */
const SessionSidebar = ({ isOpen, onClose, onSelectSession, showFFT, onToggleFFT }) => {
  const [search, setSearch] = useState('');
  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(false);

  const fetchSessions = useCallback(async (query = '') => {
    setLoading(true);
    try {
      const url = query.trim()
        ? `/api/sessions/search?q=${encodeURIComponent(query.trim())}`
        : '/api/sessions';
      const res = await fetch(url);
      const data = await res.json();
      setSessions(data.sessions || []);
    } catch (err) {
      console.error('Failed to fetch sessions', err);
      setSessions([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isOpen) {
      fetchSessions(search);
    }
  }, [isOpen, search, fetchSessions]);

  const handleSelect = (sessionId) => {
    onSelectSession(sessionId);
    onClose();
  };

  const formatTimestamp = (ts) => {
    if (!ts) return '—';
    try {
      const iso = typeof ts === 'string' && ts.includes(' ') && !ts.includes('T') ? ts.replace(' ', 'T') : ts;
      const d = new Date(iso);
      return isNaN(d.getTime()) ? ts : d.toLocaleString();
    } catch {
      return ts;
    }
  };

  return (
    <>
      <div
        className={`session-sidebar__backdrop ${isOpen ? 'session-sidebar__backdrop--visible' : ''}`}
        onClick={onClose}
        onKeyDown={(e) => e.key === 'Escape' && onClose()}
        role="button"
        tabIndex={-1}
        aria-hidden={!isOpen}
      />
      <aside
        className={`session-sidebar ${isOpen ? 'session-sidebar--open' : ''}`}
        aria-label="Session history"
      >
        <div className="session-sidebar__top-controls">
          <button
            type="button"
            className="session-sidebar__toggle"
            onClick={onToggleFFT}
          >
            {showFFT ? 'Waveform View' : 'FFT Spectrum View'}
          </button>
        </div>
        <div className="session-sidebar__header">
          <h2 className="session-sidebar__title">History</h2>
          <button
            type="button"
            className="session-sidebar__close"
            onClick={onClose}
            aria-label="Close sidebar"
          >
            ×
          </button>
        </div>
        <div className="session-sidebar__search-wrap">
          <input
            type="search"
            className="session-sidebar__search"
            placeholder="Search by session ID..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search sessions"
          />
        </div>
        <div className="session-sidebar__list-wrap">
          {loading ? (
            <p className="session-sidebar__loading">Loading…</p>
          ) : sessions.length === 0 ? (
            <p className="session-sidebar__empty">No sessions found</p>
          ) : (
            <ul className="session-sidebar__list">
              {sessions.map((s) => (
                <li key={s.session_id}>
                  <button
                    type="button"
                    className="session-sidebar__item"
                    onClick={() => handleSelect(s.session_id)}
                  >
                    <span className="session-sidebar__item-id" title={s.session_id}>
                      {s.session_id.length > 8 ? `${s.session_id.substring(0, 8)}...` : s.session_id}
                    </span>
                    <span className="session-sidebar__item-ts">
                      {formatTimestamp(s.timestamp)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>
    </>
  );
};

export default SessionSidebar;
export { HistoryIcon };
