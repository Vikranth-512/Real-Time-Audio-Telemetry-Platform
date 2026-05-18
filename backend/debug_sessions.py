#!/usr/bin/env python3
"""
Debug script to verify session storage issues.
Run this to check what's actually in the database and Redis streams.
"""

import asyncio
import os
import sys
import redis.asyncio as redis
from sqlalchemy import select, func, text
from storage.db import async_session, AudioMetric

async def check_database_sessions():
    """Check what sessions exist in database"""
    print("=== DATABASE SESSION ANALYSIS ===")
    
    async with async_session() as session:
        # 1. Count total sessions
        count_result = await session.execute()
        select(func.count(func.distinct(AudioMetric.session_id)))
        total_sessions = count_result.scalar()
        print(f"Total distinct sessions in DB: {total_sessions}")
        
        # 2. Get all session IDs
        sessions_result = await session.execute(
            select(AudioMetric.session_id, func.count(AudioMetric.id).label('count'))
            .group_by(AudioMetric.session_id)
            .order_by(func.max(AudioMetric.timestamp).desc())
        )
        sessions = sessions_result.fetchall()
        
        print("\nSession Details:")
        for session_id, count in sessions:
            print(f"  - {session_id}: {count} records")
        
        # 3. Check for device_id patterns
        device_result = await session.execute(
            select(AudioMetric.device_id, func.count(func.distinct(AudioMetric.session_id)).label('session_count'))
            .group_by(AudioMetric.device_id)
        )
        devices = device_result.fetchall()
        
        print("\nDevice Session Distribution:")
        for device_id, session_count in devices:
            print(f"  - {device_id}: {session_count} sessions")
        
        # 4. Check timestamp ranges per session
        time_result = await session.execute(
            select(
                AudioMetric.session_id,
                func.min(AudioMetric.timestamp).label('min_time'),
                func.max(AudioMetric.timestamp).label('max_time')
            ).group_by(AudioMetric.session_id)
        )
        time_ranges = time_result.fetchall()
        
        print("\nSession Time Ranges:")
        for session_id, min_time, max_time in time_ranges:
            duration = max_time - min_time
            print(f"  - {session_id}: {min_time:.2f} -> {max_time:.2f} ({duration:.2f}s)")

async def check_redis_streams():
    """Check what's in Redis streams"""
    print("\n=== REDIS STREAM ANALYSIS ===")
    
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    
    try:
        # Check audio_stream (input)
        print("\nAudio Stream (input):")
        try:
            audio_messages = await redis_client.xrange("audio_stream", "-", "+", count=10)
            session_ids = set()
            for msg_id, data in audio_messages:
                session_id = data.get("session_id", "NO_SESSION")
                session_ids.add(session_id)
                print(f"  {msg_id}: session={session_id}, device={data.get('device_id', 'NO_DEVICE')}")
            print(f"Unique sessions in audio_stream: {session_ids}")
        except Exception as e:
            print(f"Error reading audio_stream: {e}")
        
        # Check audio_metrics_stream (output)
        print("\nMetrics Stream (output):")
        try:
            metrics_messages = await redis_client.xrange("audio_metrics_stream", "-", "+", count=10)
            session_ids = set()
            for msg_id, data in metrics_messages:
                session_id = data.get("session_id", "NO_SESSION")
                session_ids.add(session_id)
                print(f"  {msg_id}: session={session_id}, device={data.get('device_id', 'NO_DEVICE')}")
            print(f"Unique sessions in audio_metrics_stream: {session_ids}")
        except Exception as e:
            print(f"Error reading audio_metrics_stream: {e}")
            
        # Check stopped sessions
        print("\nStopped Sessions:")
        try:
            stopped = await redis_client.smembers("stopped_sessions")
            print(f"Stopped sessions: {stopped}")
        except Exception as e:
            print(f"Error reading stopped_sessions: {e}")
            
    finally:
        await redis_client.aclose()

async def main():
    """Main debug function"""
    print("🔍 Session Storage Debug Tool")
    print("=" * 50)
    
    await check_database_sessions()
    await check_redis_streams()
    
    print("\n=== RECOMMENDATIONS ===")
    print("1. If you see only 1 session_id in DB:")
    print("   - Check if client is overriding server session_id")
    print("   - Verify WebSocket creates new session_id per connection")
    print("   - Ensure DB uniqueness constraints include session_id")
    
    print("\n2. If multiple sessions in Redis but 1 in DB:")
    print("   - Check DB insert logic for session_id handling")
    print("   - Verify uniqueness constraints aren't dropping valid rows")
    
    print("\n3. To enable debug logging:")
    print("   export DEBUG_MODE=true")
    print("   export LOG_LEVEL=INFO")

if __name__ == "__main__":
    asyncio.run(main())