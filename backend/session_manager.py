import time
import json
import sqlite3
import os
from typing import Dict, Optional, List, Any
from metrics_engine import MetricsEngine

DB_PATH = os.path.join(os.path.dirname(__file__), "audio_sessions.db")


def _compute_averages(metrics_list: List[Dict]) -> Dict[str, float]:
    """Compute average for each numeric key across all metric dicts."""
    if not metrics_list:
        return {
            "avg_rms": 0.0,
            "avg_peak": 0.0,
            "avg_frequency": 0.0,
            "avg_bpm": 0.0,
        }
    keys = set()
    for m in metrics_list:
        keys.update(k for k, v in m.items() if isinstance(v, (int, float)))
    sums = {k: 0.0 for k in keys}
    counts = {k: 0 for k in keys}
    for m in metrics_list:
        for k in keys:
            v = m.get(k)
            if isinstance(v, (int, float)):
                sums[k] += v
                counts[k] += 1
    result = {}
    for k in keys:
        if counts[k] > 0:
            result[f"avg_{k}"] = round(sums[k] / counts[k], 4)
    # Ensure required fields exist
    for name, key in [
        ("avg_rms", "rms"),
        ("avg_peak", "peak"),
        ("avg_frequency", "frequency"),
        ("avg_bpm", "bpm"),
    ]:
        if name not in result:
            result[name] = result.get(f"avg_{key}", 0.0) if f"avg_{key}" in result else 0.0
    return result


class SessionManager:
    def __init__(self):
        self.metrics_engine = MetricsEngine()
        self.active_sessions: Dict[str, Dict] = {}
        self.archived_sessions: Dict[str, Dict] = {}
        self._init_db()

    def _init_db(self):
        """Create SQLite table if not exists."""
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    timestamp DATETIME,
                    metrics_json TEXT,
                    averages_json TEXT
                )
            """)
            conn.commit()
        finally:
            conn.close()
        
    def create_session(self) -> str:
        """Create a new active session"""
        import uuid
        session_id = str(uuid.uuid4())
        
        self.active_sessions[session_id] = {
            'start_time': time.time(),
            'last_activity': time.time(),
            'sample_count': 0,
            'metrics_engine': MetricsEngine(),
            'connection_count': 1,
            'samples': []  # store recent metrics for export
        }
        
        print(f"Created new session: {session_id}")
        return session_id
    
    def save_session(self, session_id: str, timestamp: float, metrics_json: str, averages_json: str):
        """Persist session to SQLite."""
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO sessions (session_id, timestamp, metrics_json, averages_json)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, timestamp, metrics_json, averages_json),
            )
            conn.commit()
        finally:
            conn.close()

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Load session from DB. Returns { session_id, metrics, averages } or None."""
        conn = sqlite3.connect(DB_PATH)
        try:
            row = conn.execute(
                "SELECT session_id, timestamp, metrics_json, averages_json FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            sid, ts, mj, aj = row
            metrics = json.loads(mj) if mj else []
            averages = json.loads(aj) if aj else {}
            return {"session_id": sid, "timestamp": ts, "metrics": metrics, "averages": averages}
        finally:
            conn.close()

    def get_all_sessions(self, limit: int = 50) -> List[Dict]:
        """Return list of { session_id, timestamp } for all stored sessions."""
        conn = sqlite3.connect(DB_PATH)
        try:
            rows = conn.execute(
                "SELECT session_id, timestamp FROM sessions ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [{"session_id": r[0], "timestamp": r[1]} for r in rows]
        finally:
            conn.close()

    def search_session(self, q: str, limit: int = 50) -> List[Dict]:
        """Search sessions by session_id. Returns list of { session_id, timestamp }."""
        if not q or not q.strip():
            return self.get_all_sessions(limit=limit)
        conn = sqlite3.connect(DB_PATH)
        try:
            pattern = f"%{q.strip()}%"
            rows = conn.execute(
                "SELECT session_id, timestamp FROM sessions WHERE session_id LIKE ? ORDER BY timestamp DESC LIMIT ?",
                (pattern, limit),
            ).fetchall()
            return [{"session_id": r[0], "timestamp": r[1]} for r in rows]
        finally:
            conn.close()

    def end_session(self, session_id: str) -> bool:
        """End an active session and save final metrics to memory and SQLite."""
        if session_id not in self.active_sessions:
            return False
        
        session_info = self.active_sessions[session_id]
        metrics_engine = session_info['metrics_engine']

        # Get session summary
        summary = metrics_engine.get_session_summary()

        # Build per-second metrics list for export
        snapshots = []
        for entry in session_info.get('samples', []):
            ts, payload = entry
            if isinstance(payload, dict) and 'metrics' in payload:
                snapshots.append({'timestamp': ts, 'metrics': payload['metrics']})

        by_sec = {}
        for s in snapshots:
            sec = int(s['timestamp'])
            by_sec[sec] = s['metrics']
        times = sorted(by_sec.keys())
        full_metrics = [{'timestamp': t, 'metrics': by_sec[t]} for t in times]
        metrics_list = [by_sec[t] for t in times]
        averages = _compute_averages(metrics_list)

        # Archive session info for later export
        self.archived_sessions[session_id] = {
            'start_time': session_info['start_time'],
            'end_time': time.time(),
            'sample_count': session_info.get('sample_count', 0),
            'summary': summary,
            'samples': session_info.get('samples', []),
            'full_metrics': full_metrics,
            'averages': averages,
        }

        # Persist to SQLite
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(session_info['start_time']))
        self.save_session(
            session_id,
            timestamp,
            json.dumps(full_metrics),
            json.dumps(averages),
        )

        # Remove from active sessions
        del self.active_sessions[session_id]

        print(f"Ended session: {session_id}, archived summary: {summary}")
        return True
    
    def process_samples(self, session_id: str, samples: list, timestamp: float, bpm_simulated: float = None) -> Optional[Dict]:
        """Process samples for an active session"""
        if session_id not in self.active_sessions:
            return None
        
        session_info = self.active_sessions[session_id]
        metrics_engine = session_info['metrics_engine']
        
        # Update last activity
        session_info['last_activity'] = time.time()
        session_info['sample_count'] += len(samples)
        
        # Store samples in-memory for export
        session_info['samples'].append((timestamp, samples))
        
        # Calculate metrics
        metrics = metrics_engine.calculate_metrics(samples, timestamp)

        # store last observed frequency for active session listing
        if 'frequency' in metrics and metrics['frequency']:
            session_info['last_frequency'] = metrics['frequency']

        # If the simulator provided an explicit BPM, prefer that for immediate UI feedback
        if bpm_simulated is not None:
            try:
                bpm_val = float(bpm_simulated)
                if bpm_val > 0:
                    metrics['bpm'] = bpm_val
                    # remember last bpm for session summary
                    session_info['last_bpm'] = bpm_val
            except Exception:
                pass
        # remember frequency for active session
        if 'frequency' in metrics:
            session_info['last_frequency'] = metrics['frequency']

        # Also store per-second aggregated metrics for export
        # We'll sample at ~1s resolution: append current metrics snapshot
        session_info['samples'].append((timestamp, {'metrics': metrics}))
        
        return metrics
    
    def get_active_session(self, session_id: str) -> Optional[Dict]:
        """Get active session info"""
        return self.active_sessions.get(session_id)
    
    def is_session_active(self, session_id: str) -> bool:
        """Check if session is active"""
        return session_id in self.active_sessions
    
    def cleanup_inactive_sessions(self, timeout_seconds: int = 60):
        """Clean up sessions that have been inactive"""
        current_time = time.time()
        inactive_sessions = []
        
        for session_id, session_info in self.active_sessions.items():
            if current_time - session_info['last_activity'] > timeout_seconds:
                inactive_sessions.append(session_id)
        
        for session_id in inactive_sessions:
            self.end_session(session_id)
            print(f"Cleaned up inactive session: {session_id}")
    
    def get_session_stats(self, session_id: str) -> Optional[Dict]:
        """Get statistics for a specific session"""
        if session_id in self.active_sessions:
            # Active session stats
            session_info = self.active_sessions[session_id]
            metrics_engine = session_info['metrics_engine']
            
            current_time = time.time()
            duration = current_time - session_info['start_time']
            
            current = metrics_engine.get_session_summary()
            # include last observed frequency if available
            if 'last_frequency' in session_info:
                current['frequency'] = session_info.get('last_frequency')

            return {
                'session_id': session_id,
                'status': 'active',
                'duration': duration,
                # include start_time in milliseconds so front-end can use it directly
                'start_time': int(session_info['start_time'] * 1000),
                'sample_count': session_info['sample_count'],
                'current_metrics': current
            }
        else:
            # Historical session from DB
            session_data = self.get_session(session_id)
            if session_data:
                av = session_data.get('averages', {})
                return {
                    'session_id': session_id,
                    'status': 'completed',
                    'current_metrics': {
                        'avg_bpm': av.get('avg_bpm', 0),
                        'max_amplitude': av.get('avg_peak', 0),
                        'avg_rms': av.get('avg_rms', 0)
                    }
                }
        
        return None
    
    def get_all_sessions_merged(self, limit: int = 50):
        """Get all sessions (active and historical) for dashboard listing."""
        active_sessions = []
        for session_id in self.active_sessions:
            stats = self.get_session_stats(session_id)
            if stats:
                active_sessions.append(stats)
        historical = self.get_all_sessions(limit=limit)
        return {
            'active': active_sessions,
            'historical': historical
        }
    
    def get_current_metrics(self, session_id: str) -> Optional[Dict]:
        """Get current metrics for an active session"""
        if session_id not in self.active_sessions:
            return None
        
        metrics_engine = self.active_sessions[session_id]['metrics_engine']
        return metrics_engine.get_session_summary()

    def get_session_export(self, session_id: str) -> Optional[Dict]:
        """Return session export with averages and full_metrics: { session_id, averages, full_metrics }."""
        # 1) Archived (just ended): use stored full_metrics and averages
        if session_id in self.archived_sessions:
            session_info = self.archived_sessions[session_id]
            return {
                'session_id': session_id,
                'start_time': session_info['start_time'],
                'averages': session_info.get('averages', {}),
                'full_metrics': session_info.get('full_metrics', [])
            }
        # 2) From DB (persisted)
        from_db = self.get_session(session_id)
        if from_db:
            return {
                'session_id': from_db['session_id'],
                'averages': from_db.get('averages', {}),
                'full_metrics': from_db.get('metrics', [])
            }
        # 3) Active session: build from current samples and compute averages
        if session_id in self.active_sessions:
            session_info = self.active_sessions[session_id]
            snapshots = []
            for entry in session_info.get('samples', []):
                ts, payload = entry
                if isinstance(payload, dict) and 'metrics' in payload:
                    snapshots.append({'timestamp': ts, 'metrics': payload['metrics']})
            by_sec = {}
            for s in snapshots:
                sec = int(s['timestamp'])
                by_sec[sec] = s['metrics']
            times = sorted(by_sec.keys())
            full_metrics = [{'timestamp': t, 'metrics': by_sec[t]} for t in times]
            metrics_list = [by_sec[t] for t in times]
            averages = _compute_averages(metrics_list)
            return {
                'session_id': session_id,
                'start_time': session_info['start_time'],
                'averages': averages,
                'full_metrics': full_metrics
            }
        return None
