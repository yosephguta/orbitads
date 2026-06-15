from __future__ import annotations

import io
from typing import Optional

from app.core.config import get_settings

settings = get_settings()


async def get_audio_level(s3_key: str) -> Optional[float]:
    """Download audio/video from S3 and return average dBFS level."""
    try:
        import boto3
        from pydub import AudioSegment

        s3 = boto3.client(
            "s3",
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_region,
        )
        obj = s3.get_object(Bucket=settings.s3_bucket_name, Key=s3_key)
        data = obj["Body"].read()

        try:
            segment = AudioSegment.from_file(io.BytesIO(data), format="mp4")
        except Exception:
            segment = AudioSegment.from_file(io.BytesIO(data))

        return segment.dBFS
    except Exception as e:
        print(f"Could not measure audio level: {e}")
        return None


async def calculate_volume_multiplier(
    target_dBFS: float,
    source_dBFS: float,
) -> float:
    """Calculate Shotstack volume multiplier to match target level."""
    if source_dBFS is None or source_dBFS <= -100:
        return 2.0

    diff = target_dBFS - source_dBFS
    multiplier = 10 ** (diff / 20)
    return max(0.5, min(4.0, multiplier))
