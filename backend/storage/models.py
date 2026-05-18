from sqlalchemy import Column, Integer, Float, String, UniqueConstraint, Index
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class AudioMetric(Base):
    __tablename__ = "audio_metrics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(Float, nullable=False)
    device_id = Column(String, nullable=False)
    session_id = Column(String, nullable=False)
    bpm = Column(Float)
    avg_amplitude = Column(Float)
    rms_energy = Column(Float)
    zcr = Column(Float)
    frequency = Column(Float)

    __table_args__ = (
        UniqueConstraint('device_id', 'timestamp', 'session_id', name='uq_device_time_session'),
        Index('idx_device_time', 'device_id', 'timestamp', postgresql_ops={'timestamp': 'DESC'}),
        Index('idx_session_id', 'session_id'),
    )
