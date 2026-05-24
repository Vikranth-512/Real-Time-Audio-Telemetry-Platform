import { useState, useEffect, useRef, useCallback } from 'react';

/**
 * useWebSocket — single multiplexed WebSocket connection.
 *
 * Architecture:
 *   ONE ws connection to /ws/audio
 *   N session subscriptions (subscribe / unsubscribe via messages)
 *   Internal routing: incoming packets dispatched by session_id
 *
 * Global listeners (session_discovered, session_ended, active_sessions)
 * are routed to all global subscribers (no session filter).
 */

const GLOBAL_CHANNEL = '__global__';

export const useWebSocket = (url) => {
    const [isConnected, setIsConnected]       = useState(false);
    const [connectionStatus, setConnectionStatus] = useState('Disconnected');

    const ws                 = useRef(null);
    const reconnectTimeout   = useRef(null);
    const reconnectAttempts  = useRef(0);
    const maxReconnect       = 5;

    // session_id -> Set<callback>  |  GLOBAL_CHANNEL -> Set<callback>
    const subscribers = useRef(new Map());

    // ── Internal dispatch ──────────────────────────────────────────────────
    const dispatch = useCallback((raw) => {
        let data;
        try { data = JSON.parse(raw); } catch { return; }

        if (data.type === 'audio_update' && data.samples) {
            // Phase 7: Stale packet rejection
            if (data.capture_timestamp) {
                const latency = Date.now() / 1000 - data.capture_timestamp;
                if (latency > 0.5) return; // Drop stale packets
                data.latency = latency;
            }
            // Phase 14: Avoid JSON parsing on render path (keep Float32Array instead of JS array)
            data.samples = new Float32Array(data.samples);
        }

        const sid = data.session_id;

        // Route to session-specific callbacks
        if (sid) {
            const set = subscribers.current.get(sid);
            if (set) {
                set.forEach(cb => { try { cb(data); } catch (e) { console.error(e); } });
            }
        }

        // Global events always go to the global channel
        const globalTypes = new Set([
            'session_discovered', 'session_ended', 'active_sessions', 'pong',
        ]);
        if (!sid || globalTypes.has(data.type)) {
            const set = subscribers.current.get(GLOBAL_CHANNEL);
            if (set) {
                set.forEach(cb => { try { cb(data); } catch (e) { console.error(e); } });
            }
        }
    }, []);

    // ── Send helper ────────────────────────────────────────────────────────
    const send = useCallback((message) => {
        if (ws.current && ws.current.readyState === WebSocket.OPEN) {
            ws.current.send(typeof message === 'string' ? message : JSON.stringify(message));
            return true;
        }
        return false;
    }, []);

    // ── Connect ────────────────────────────────────────────────────────────
    const connect = useCallback(() => {
        try {
            ws.current = new WebSocket(url);

            ws.current.onopen = () => {
                setIsConnected(true);
                setConnectionStatus('Connected');
                reconnectAttempts.current = 0;
                console.log('[WS] Connected');
            };

            ws.current.onmessage = (event) => dispatch(event.data);

            ws.current.onclose = (event) => {
                setIsConnected(false);
                setConnectionStatus('Disconnected');
                if (event.code !== 1000 && reconnectAttempts.current < maxReconnect) {
                    reconnectAttempts.current++;
                    const delay = Math.pow(2, reconnectAttempts.current) * 1000;
                    setConnectionStatus(`Reconnecting… (${reconnectAttempts.current}/${maxReconnect})`);
                    reconnectTimeout.current = setTimeout(connect, delay);
                } else if (reconnectAttempts.current >= maxReconnect) {
                    setConnectionStatus('Connection failed');
                }
            };

            ws.current.onerror = () => setConnectionStatus('Error');

        } catch (err) {
            console.error('[WS] Failed to connect:', err);
            setConnectionStatus('Connection failed');
        }
    }, [url, dispatch]);

    const disconnect = useCallback(() => {
        clearTimeout(reconnectTimeout.current);
        if (ws.current) {
            ws.current.close(1000, 'User disconnect');
            ws.current = null;
        }
        setIsConnected(false);
        setConnectionStatus('Disconnected');
        reconnectAttempts.current = 0;
    }, []);

    // ── Subscribe helpers ──────────────────────────────────────────────────

    /**
     * Subscribe to global events (session_discovered, session_ended, active_sessions).
     * Returns an unsubscribe function.
     */
    const subscribe = useCallback((callback) => {
        if (!subscribers.current.has(GLOBAL_CHANNEL)) {
            subscribers.current.set(GLOBAL_CHANNEL, new Set());
        }
        subscribers.current.get(GLOBAL_CHANNEL).add(callback);
        return () => {
            const set = subscribers.current.get(GLOBAL_CHANNEL);
            if (set) { set.delete(callback); if (!set.size) subscribers.current.delete(GLOBAL_CHANNEL); }
        };
    }, []);

    /**
     * Subscribe to a specific session's audio_update packets.
     * Sends a subscribe message to the backend and registers the callback.
     * Returns an unsubscribe function.
     */
    const subscribeToSession = useCallback((sessionId, callback) => {
        if (!subscribers.current.has(sessionId)) {
            subscribers.current.set(sessionId, new Set());
        }
        subscribers.current.get(sessionId).add(callback);

        // Tell backend we want this session's data
        send({ type: 'subscribe', session_id: sessionId });

        return () => {
            const set = subscribers.current.get(sessionId);
            if (set) {
                set.delete(callback);
                if (!set.size) {
                    subscribers.current.delete(sessionId);
                    send({ type: 'unsubscribe', session_id: sessionId });
                }
            }
        };
    }, [send]);

    /**
     * Legacy raw-string subscriber (backward compat with LiveAcousticPanel).
     * Wraps any session id callback that expects raw JSON strings.
     */
    const subscribeRaw = useCallback((callback) => {
        // Subscribe to ALL sessions by intercepting before dispatch
        // We wrap the ws.onmessage directly here for raw access
        const legacyCb = (event) => callback(event.data);
        if (ws.current) {
            const prev = ws.current.onmessage;
            ws.current.onmessage = (event) => {
                dispatch(event.data);
                legacyCb(event);
            };
        }
        // Also wire into global subscribers as a passthrough
        const globalCb = (data) => callback(JSON.stringify(data));
        const sessionCb = (data) => {
            if (data.type === 'audio_update') callback(JSON.stringify(data));
        };

        // Register as global
        if (!subscribers.current.has(GLOBAL_CHANNEL)) {
            subscribers.current.set(GLOBAL_CHANNEL, new Set());
        }
        subscribers.current.get(GLOBAL_CHANNEL).add(sessionCb);

        return () => {
            const set = subscribers.current.get(GLOBAL_CHANNEL);
            if (set) set.delete(sessionCb);
        };
    }, [dispatch]);

    // ── Lifecycle ──────────────────────────────────────────────────────────
    useEffect(() => {
        connect();
        return () => { disconnect(); };
    }, [connect, disconnect]);

    // Native WebSocket heartbeat via ping every 30s
    useEffect(() => {
        if (!isConnected) return;
        const id = setInterval(() => {
            if (ws.current && ws.current.readyState === WebSocket.OPEN) {
                ws.current.ping?.(); // native WS ping (browser may ignore)
                send({ type: 'ping' });  // application-level fallback
            }
        }, 30_000);
        return () => clearInterval(id);
    }, [isConnected, send]);

    return {
        isConnected,
        connectionStatus,
        send,
        subscribe,          // global events
        subscribeToSession, // session-scoped audio_update packets
        subscribeRaw,       // legacy compat
        disconnect,
        reconnect: connect,
    };
};
